"""Independent local-practice facts and stable user-code checkpoints."""
from __future__ import annotations

import hashlib, json, os, re, shutil, stat, uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from jsonschema import Draft202012Validator, FormatChecker

from .command_response import response
from .manifest import atomic_write_json, read_json
from .package_lock import package_lock
from .workspace import PORTABLE_SCHEMA_VERSION, WorkspaceConfig, WorkspaceError, discover_workspace

SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas/practice-snapshot-v1.schema.json").read_text())
EVENT_SCHEMA = json.loads((Path(__file__).resolve().parent.parent / "schemas/practice-event-v1.schema.json").read_text())
ID_RE = re.compile(r"^practice-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ROLES = ("example", "task", "tests", "reference")

def _now() -> str: return datetime.now(timezone.utc).isoformat()

def _root(config: WorkspaceConfig) -> Path:
    if config.schema_version != PORTABLE_SCHEMA_VERSION or config.results is None:
        raise WorkspaceError("practice v1 requires workspace schema v2")
    root = config.results / "practices"
    if root.is_symlink() or root.resolve(strict=False) != config.results.resolve(strict=False) / "practices":
        raise WorkspaceError(f"refusing symbolic link inside declared results root: {root}")
    for name in ("objects", "commits", "current.json", "candidates", "workspaces"):
        if (root / name).is_symlink(): raise WorkspaceError(f"refusing symbolic link in practice store: {root/name}")
    return root

def _object(root: Path, digest: str) -> Path:
    path = root / "objects" / digest[:2] / digest
    if path.parent.is_symlink() or path.is_symlink(): raise WorkspaceError("refusing symbolic link in practice object store")
    return path

def _empty() -> dict[str, Any]:
    return {"schema_version":1,"commit_id":None,"parent_commit_id":None,"revision":0,"created_at":None,"practices":{},"objects":{}}

def _validate(value: dict[str, Any]) -> None:
    errors = list(Draft202012Validator(SCHEMA, format_checker=FormatChecker()).iter_errors(value))
    if errors: raise WorkspaceError(f"practice-snapshot-v1 validation failed: {errors[0].message}")
    for key, practice in value["practices"].items():
        if practice.get("practice_id") != key: raise WorkspaceError(f"practice identity mismatch: {key}")
        for event in practice.get("events",[]):
            recorded_at=event.get("recorded_at")
            if not isinstance(recorded_at,str):raise WorkspaceError("practice event recorded_at is required")
            candidate={name:item for name,item in event.items() if name not in {"recorded_at"}}
            if event.get("kind")!="test":
                if candidate.pop("verification",None)!="reported_not_executed":raise WorkspaceError("non-test practice event verification is invalid")
            event_errors=list(Draft202012Validator(EVENT_SCHEMA,format_checker=FormatChecker()).iter_errors(candidate))
            if event_errors:raise WorkspaceError(f"stored practice event is invalid: {event_errors[0].message}")
        reachable = [practice["preparation_sha256"], *[x["object_sha256"] for x in practice["checkpoints"]]]
        if any(digest not in value["objects"] for digest in reachable): raise WorkspaceError("practice object is unreachable")

def _load(config: WorkspaceConfig) -> dict[str, Any]:
    root = _root(config); pointer = root / "current.json"
    if not pointer.is_file(): return _empty()
    selected = read_json(pointer); manifest = root / "commits" / f'{selected["commit_id"]}.json'
    if not manifest.is_file() or hashlib.sha256(manifest.read_bytes()).hexdigest() != selected.get("manifest_sha256"):
        raise WorkspaceError("practice snapshot manifest is missing or corrupt")
    value = read_json(manifest); _validate(value)
    for digest, metadata in value["objects"].items():
        path = _object(root, digest)
        if not path.is_file() or path.stat().st_size != metadata["size"] or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise WorkspaceError(f"practice object is missing or corrupt: {digest}")
    return value

def _sync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)

def _put(root: Path, body: bytes) -> tuple[str,int]:
    digest = hashlib.sha256(body).hexdigest(); path = _object(root,digest); path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        with path.open("xb") as stream: stream.write(body); stream.flush(); os.fsync(stream.fileno())
        _sync(path.parent)
    return digest,len(body)

def _publish(config: WorkspaceConfig, old: dict[str,Any], practices: dict[str,Any], objects: dict[str,Any]) -> dict[str,Any]:
    root = _root(config)
    value={"schema_version":1,"parent_commit_id":old.get("commit_id"),"revision":old["revision"]+1,
           "created_at":_now(),"practices":practices,"objects":{**old.get("objects",{}),**objects}}
    encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
    value["commit_id"]="practice-commit-"+hashlib.sha256(encoded).hexdigest(); _validate(value)
    body=json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True).encode()+b"\n"
    commits=root/"commits"; commits.mkdir(parents=True,exist_ok=True); manifest=commits/f'{value["commit_id"]}.json'
    if not manifest.exists():
        with manifest.open("xb") as stream: stream.write(body); stream.flush(); os.fsync(stream.fileno())
    _sync(commits); atomic_write_json(root/"current.json",{"schema_version":1,"commit_id":value["commit_id"],"manifest_sha256":hashlib.sha256(body).hexdigest()}); _sync(root)
    return value

def _request(request: dict[str,Any]) -> dict[str,dict[str,str]]:
    if request.get("schema_version")!=1 or not ID_RE.fullmatch(str(request.get("practice_id",""))): raise ValueError("schema_version 1 and UUIDv4 practice_id required")
    for key in ("question_id","title","scope","simulation_scope"):
        if not isinstance(request.get(key),str) or not request[key].strip(): raise ValueError(f"{key} is required")
    if not isinstance(request.get("effort_minutes"),int) or not 1<=request["effort_minutes"]<=120: raise ValueError("effort_minutes must be 1..120")
    criteria=request.get("completion_criteria")
    if not isinstance(criteria,list) or not 1<=len(criteria)<=5 or not all(isinstance(x,str) and x.strip() for x in criteria): raise ValueError("one to five completion criteria required")
    command=request.get("test_command")
    if not isinstance(command,list) or not command or not all(isinstance(x,str) and x for x in command): raise ValueError("test_command must be argv; it is recorded, never executed")
    files=request.get("files")
    if not isinstance(files,dict) or set(files)!=set(ROLES): raise ValueError("files must separate example, task, tests, and reference")
    for role,mapping in files.items():
        if not isinstance(mapping,dict) or not mapping: raise ValueError(f"{role} requires files")
        for name,content in mapping.items():
            path=PurePosixPath(name)
            if not isinstance(name,str) or path.is_absolute() or ".." in path.parts or not name: raise ValueError(f"unsafe practice path: {name!r}")
            if not isinstance(content,str): raise ValueError(f"practice file must be text: {name}")
    return files

def _result(config:WorkspaceConfig,snapshot:dict[str,Any],practice:dict[str,Any],**extra:Any)->dict[str,Any]:
    return response(status="completed",workspace=str(config.config_path),
        result={"practice_schema_version":1,"revision":snapshot["revision"],"commit_id":snapshot["commit_id"],"practice":practice,
                "workspace":str(_root(config)/"workspaces"/practice["practice_id"]),**extra},validation={"practice_snapshot":"passed"})

def prepare(config:WorkspaceConfig,request_path:Path)->dict[str,Any]:
    request=read_json(request_path); files=_request(request); root=_root(config)
    with package_lock(root):
        snapshot=_load(config); pid=request["practice_id"]
        body=json.dumps(request,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
        if pid in snapshot["practices"]:
            existing=snapshot["practices"][pid]
            if hashlib.sha256(body).hexdigest()!=existing["preparation_sha256"]:
                raise ValueError("practice_id already belongs to a different preparation; existing user work was preserved")
            return _result(config,snapshot,existing,reused=True)
        workspace=root/"workspaces"/pid
        if workspace.exists():
            expected={(Path(role)/relative).as_posix():content.encode() for role,mapping in files.items() for relative,content in mapping.items()}
            observed={path.relative_to(workspace).as_posix():path.read_bytes() for path in workspace.rglob("*") if path.is_file() and not path.is_symlink()}
            if any(path.is_symlink() for path in workspace.rglob("*")) or observed!=expected:
                raise WorkspaceError(f"preserving unregistered or changed practice workspace: {workspace}")
        else:
            parent=workspace.parent; parent.mkdir(parents=True,exist_ok=True)
            staging=parent/f".{pid}.{uuid.uuid4()}.staging"
            try:
                staging.mkdir()
                for role,mapping in files.items():
                    for relative,content in mapping.items():
                        destination=staging/role/relative; destination.parent.mkdir(parents=True,exist_ok=True)
                        with destination.open("x",encoding="utf-8") as stream:
                            stream.write(content); stream.flush(); os.fsync(stream.fileno())
                os.replace(staging,workspace); _sync(parent)
            except Exception:
                shutil.rmtree(staging,ignore_errors=True)
                raise
        digest,size=_put(root,body)
        practice={key:request[key] for key in ("practice_id","question_id","title","scope","effort_minutes","completion_criteria","simulation_scope","test_command")}
        practice.update({"created_at":_now(),"preparation_sha256":digest,"events":[],"checkpoints":[],"completion":"in_progress","next_step":"write task code"})
        published=_publish(config,snapshot,{**snapshot["practices"],pid:practice},{digest:{"kind":"practice_preparation","size":size}})
        return _result(config,published,practice,reused=False)

def _get(snapshot:dict[str,Any],pid:str)->dict[str,Any]:
    if pid not in snapshot["practices"]: raise ValueError(f"unknown practice_id: {pid}")
    return snapshot["practices"][pid]

def _conflict(config:WorkspaceConfig,snapshot:dict[str,Any],expected:int,operation:str,proposal:dict[str,Any])->dict[str,Any]|None:
    if expected==snapshot["revision"]: return None
    directory=_root(config)/"candidates"; directory.mkdir(parents=True,exist_ok=True); path=directory/f"candidate-{uuid.uuid4()}.json"
    atomic_write_json(path,{"schema_version":1,"operation":operation,"expected_revision":expected,"observed_revision":snapshot["revision"],"proposal":proposal}); _sync(directory)
    return response(status="awaiting_user",workspace=str(config.config_path),result={"candidate":str(path),"observed_revision":snapshot["revision"]},validation={"expected_revision":"conflict"},next_action={"type":"user","reason":"resolve practice revision conflict"})

def _open_task(root:Path,pid:str)->int:
    descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in ("workspaces",pid,"task"):
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor); descriptor=child
        return descriptor
    except Exception:
        os.close(descriptor); raise

def _inventory(task_fd:int)->dict[str,tuple[str,int,int,int,int]]:
    result:dict[str,tuple[str,int,int,int,int]]={}
    def walk(fd:int,prefix:PurePosixPath)->None:
        with os.scandir(fd) as entries:
            for entry in entries:
                relative=(prefix/entry.name).as_posix(); metadata=entry.stat(follow_symlinks=False); mode=metadata.st_mode
                kind="file" if stat.S_ISREG(mode) else "dir" if stat.S_ISDIR(mode) else "symlink" if stat.S_ISLNK(mode) else "other"
                result[relative]=(kind,metadata.st_dev,metadata.st_ino,metadata.st_size,metadata.st_mtime_ns)
                if kind=="dir":
                    child=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    try: walk(child,prefix/entry.name)
                    finally: os.close(child)
    walk(task_fd,PurePosixPath())
    return result

def _read_regular(task_fd:int,relative:str)->bytes:
    parts=PurePosixPath(relative).parts; descriptor=os.dup(task_fd)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            os.close(descriptor); descriptor=child
        file_descriptor=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=descriptor)
        try:
            before=os.fstat(file_descriptor)
            if not stat.S_ISREG(before.st_mode): raise WorkspaceError(f"editable path changed type: {relative}")
            chunks=[]
            while True:
                chunk=os.read(file_descriptor,1024*1024)
                if not chunk: break
                chunks.append(chunk)
            after=os.fstat(file_descriptor)
            signature=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
            if signature(before)!=signature(after): raise WorkspaceError(f"editable file changed while reading: {relative}")
            return b"".join(chunks)
        finally: os.close(file_descriptor)
    finally: os.close(descriptor)

def _unstable(config:WorkspaceConfig,snapshot:dict[str,Any],pid:str,diagnostic:str)->dict[str,Any]:
    return response(status="recoverable_failure",workspace=str(config.config_path),result={"practice_id":pid,"revision":snapshot["revision"]},validation={"stable_code":"failed"},diagnostics=[diagnostic],next_action={"type":"retry","reason":"save or pause edits, then checkpoint again"})

def _derive_completion(practice:dict[str,Any])->str:
    events=practice["events"]
    attempts=any(item["kind"]=="attempt" for item in events)
    if not practice["checkpoints"] or not attempts:return "in_progress"
    checkpoint=practice["checkpoints"][-1]
    observed_tests=[item for item in events if item["kind"]=="test"
                    and item.get("verification")=="tool_observed"
                    and item.get("checkpoint_id")==checkpoint["checkpoint_id"]
                    and item.get("code_object_sha256")==checkpoint["object_sha256"]]
    if not observed_tests:return "in_progress"
    latest=observed_tests[-1]
    passed=latest["exit_code"]==0 and all(value=="passed" for value in latest["cases"].values())
    if not passed:return "in_progress"
    return "with_hint" if any(item["kind"]=="hint" for item in events) else "independent"

def checkpoint(config:WorkspaceConfig,pid:str,expected_revision:int)->dict[str,Any]:
    root=_root(config)
    with package_lock(root):
        snapshot=_load(config); practice=_get(snapshot,pid); conflict=_conflict(config,snapshot,expected_revision,"checkpoint",{"practice_id":pid})
        if conflict:return conflict
        workspace=root/"workspaces"/pid; task=workspace/"task"; task_fd=None
        try:
            task_fd=_open_task(root,pid); initial_root=os.fstat(task_fd)
            before=_inventory(task_fd)
            if not before or any(item[0] not in {"file","dir"} for item in before.values()): raise WorkspaceError("editable paths must be regular files or directories")
            file_names=sorted(name for name,item in before.items() if item[0]=="file")
            if not file_names:raise WorkspaceError("editable task contains no regular files")
            fault=os.environ.get("VIDEO_EXTRACT_PRACTICE_TEST_FAULT")
            target=task/file_names[0]
            if fault=="late_file":(task/"late.py").write_text("# late\n")
            elif fault=="remove_file":target.unlink()
            elif fault=="type_change":target.unlink();target.mkdir()
            elif fault=="symlink_swap":target.unlink();target.symlink_to(workspace/"reference/solution.py")
            elif fault=="unstable_read":target.write_bytes(target.read_bytes()+b"\n# concurrent edit")
            first=[(name,_read_regular(task_fd,name)) for name in file_names]
            fresh_fd=_open_task(root,pid)
            try:
                fresh_root=os.fstat(fresh_fd)
                if (initial_root.st_dev,initial_root.st_ino)!=(fresh_root.st_dev,fresh_root.st_ino):raise WorkspaceError("editable task root changed during checkpoint")
                after=_inventory(fresh_fd)
            finally:os.close(fresh_fd)
            if before!=after: raise WorkspaceError("editable file set, type, or metadata changed during checkpoint")
            second=[(name,_read_regular(task_fd,name)) for name in file_names]
            if first!=second: raise WorkspaceError("editable file content changed during checkpoint")
            if fault=="late_after_second":(task/"late.py").write_text("# late\n")
            final_fd=_open_task(root,pid)
            try:
                final_root=os.fstat(final_fd)
                if (initial_root.st_dev,initial_root.st_ino)!=(final_root.st_dev,final_root.st_ino):raise WorkspaceError("editable task root changed during checkpoint")
                final=_inventory(final_fd)
            finally:os.close(final_fd)
            if after!=final: raise WorkspaceError("editable file set, type, or metadata changed during checkpoint")
            payload=json.dumps({name:body.decode("utf-8") for name,body in first},ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
        except (OSError,WorkspaceError) as exc:
            return _unstable(config,snapshot,pid,str(exc))
        except UnicodeError as exc:
            return _unstable(config,snapshot,pid,f"editable task is not UTF-8 text: {exc}")
        finally:
            if task_fd is not None:os.close(task_fd)
        digest,size=_put(root,payload)
        item={"checkpoint_id":"checkpoint-"+str(uuid.uuid4()),"created_at":_now(),"object_sha256":digest,"files":[{"path":name,"sha256":hashlib.sha256(body).hexdigest(),"size":len(body)} for name,body in first]}
        updated={**practice,"checkpoints":[*practice["checkpoints"],item],"next_step":"run isolated tests"}
        updated["completion"]=_derive_completion(updated)
        published=_publish(config,snapshot,{**snapshot["practices"],pid:updated},{digest:{"kind":"code_checkpoint","size":size}})
        return _result(config,published,updated,checkpoint=item)

def record(config:WorkspaceConfig,pid:str,request_path:Path,expected_revision:int)->dict[str,Any]:
    event=read_json(request_path)
    errors=list(Draft202012Validator(EVENT_SCHEMA,format_checker=FormatChecker()).iter_errors(event))
    if errors:raise ValueError(f"practice-event-v1 validation failed: {errors[0].message}")
    root=_root(config)
    with package_lock(root):
        snapshot=_load(config); practice=_get(snapshot,pid); conflict=_conflict(config,snapshot,expected_revision,"record",{"practice_id":pid,"event":event})
        if conflict:return conflict
        if any(x["event_id"]==event["event_id"] for x in practice["events"]):return _result(config,snapshot,practice,reused=True)
        if event["kind"]=="test":
            checkpoints={item["checkpoint_id"]:item for item in practice["checkpoints"]}
            checkpoint=checkpoints.get(event["checkpoint_id"])
            if checkpoint is None or checkpoint["object_sha256"]!=event["code_object_sha256"]:
                raise ValueError("test event must identify an existing checkpoint and its code object")
        saved={**event,"recorded_at":_now()}
        if event["kind"]!="test":saved["verification"]="reported_not_executed"
        updated={**practice,"events":[*practice["events"],saved]}
        if event["kind"]=="outcome":updated.update({"reported_outcome":event["completion"],"next_step":event["next_step"]})
        updated["completion"]=_derive_completion(updated)
        published=_publish(config,snapshot,{**snapshot["practices"],pid:updated},{})
        return _result(config,published,updated,event=saved)

def backup_entries(config:WorkspaceConfig)->dict[str,Any]:
    snapshot=_load(config); root=_root(config)
    if snapshot["commit_id"] is None:return {"store":"practice-snapshot-v1","commit_id":None,"entries":[]}
    return {"store":"practice-snapshot-v1","commit_id":snapshot["commit_id"],"entries":[str(root/"current.json"),str(root/"commits"/f'{snapshot["commit_id"]}.json'),*[str(_object(root,d)) for d in sorted(snapshot["objects"])]]}

def run_practice(request:dict[str,Any])->dict[str,Any]:
    config=discover_workspace(Path(request["workspace"])); action=request.get("action")
    if action=="prepare":return prepare(config,Path(request["request"]))
    if action=="checkpoint":return checkpoint(config,request["practice_id"],request["expected_revision"])
    if action=="record":return record(config,request["practice_id"],Path(request["request"]),request["expected_revision"])
    raise ValueError("action must be prepare, checkpoint, or record")
run_practice.__capability_contract__={"input_type":"practice-request-v1","output_type":"command-response-v1"}
