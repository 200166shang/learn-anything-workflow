"""Schema-v5 extraction orchestration."""
from __future__ import annotations
from pathlib import Path
import time
from .contracts import V5_ARTIFACTS
from .manifest import atomic_write_json, fingerprint, read_json, sanitize
from .media import MediaMaterializer, resolve_inventory, select_audio_stream
from .media_request import MediaRequest
from .package_paths import canonical_package
from .planner import build_media_plan

def plan(source: str, request: MediaRequest, package: Path | None = None):
    inventory = resolve_inventory(source); result = build_media_plan(inventory, request)
    # Planning is intentionally usable without a configured workspace because it
    # performs no writes. Ensure resolves the canonical destination before writing.
    result["package"] = str(package) if package else None; return result

def ensure(source: str, request: MediaRequest, package: Path | None = None):
    inventory = resolve_inventory(source, allow_browser=True)
    package = (package or canonical_package(inventory)).expanduser().resolve(); package.mkdir(parents=True, exist_ok=True)
    manifest_path = package / "manifest.json"; data = read_json(manifest_path) if manifest_path.exists() else {}
    data.update({"schema_version":5,"platform":inventory.platform,"identity":inventory.identity,"title":inventory.title,
      "source_url":sanitize(source),"request":{"type":"media",**request.to_dict()},"artifacts":data.get("artifacts",{}),
      "provenance":data.get("provenance",{}),"stages":data.get("stages",{})})
    materializer=MediaMaterializer(audio_bitrate="192k" if request.quality=="high" else "128k")
    planned=build_media_plan(inventory,request,_existing(package,data))
    for stage in planned["planned_stages"]:
        name=stage["name"]; started=time.monotonic()
        if name=="resolve_source": continue
        if name=="materialize_source_audio":
            target=package/V5_ARTIFACTS["source_audio"]; stream=select_audio_stream(inventory,request.quality)
            if not stream: raise RuntimeError("source has no materializable audio stream")
            if inventory.platform=="local": materializer.audio_from_local(Path(source),target)
            else: materializer.audio_from_url(
                inventory.source if inventory.platform=="youtube" else stream,
                target,
                format_id=stream.id if inventory.platform=="youtube" else None,
            )
            _record(data,"source_audio",target,package,{"stream_id":stream.id,"language":stream.language,"kind":"source_audio"},started); atomic_write_json(manifest_path,data)
        elif name=="materialize_source_video":
            target=package/V5_ARTIFACTS["source_video"]; stream=max((x for x in inventory.video_streams if x.url),key=lambda x:x.height or 0,default=None)
            if inventory.platform=="local": materializer.copy_local_video(Path(source),target)
            elif stream: materializer.video_from_url(stream,target)
            else: raise RuntimeError("source has no materializable video stream")
            _record(data,"source_video",target,package,{"stream_id":stream.id if stream else "local","kind":"source_video"},started)
        elif name=="materialize_best_subtitle":
            track=next((x for x in inventory.subtitles if not x.automatic and x.url),None) or next((x for x in inventory.subtitles if x.url),None)
            if track:
                target=package/"subtitles"/f"source.{track.language}.srt"; materializer.subtitle_from_url(track,target)
                _record(data,"source_subtitle",target,package,{"track_id":track.id,"language":track.language,"automatic":track.automatic},started)
            else: data.setdefault("results",{})["subtitles"]={"available":False,"reason":"platform reported no subtitle track"}
        elif name=="normalize_native_chinese_audio":
            stream=select_audio_stream(inventory,request.quality,"zh"); target=package/V5_ARTIFACTS["localized_audio"]
            materializer.audio_from_url(
                inventory.source if inventory.platform=="youtube" else stream,
                target,
                format_id=stream.id if inventory.platform=="youtube" else None,
            )
            _record(data,"localized_audio",target,package,{"kind":"native_chinese_track","language":stream.language,"stream_id":stream.id},started)
    mode=(planned.get("mandarin_audio") or {}).get("mode")
    if mode=="external_pyvideotrans":
        data["pause"]={"status":"awaiting_external","capability":"pyvideotrans_podcast","input_artifact":V5_ARTIFACTS["source_audio"],"message":"Run pyVideoTrans podcast mode through the extract-media workflow."}; atomic_write_json(manifest_path,data)
        return {"status":"awaiting_external","package":str(package),"mandarin_audio":planned["mandarin_audio"],"blocker":data["pause"]["message"]}
    if mode=="unsupported":
        data["pause"]={"status":"unsupported",**planned["mandarin_audio"]}; atomic_write_json(manifest_path,data)
        return {"status":"unsupported","package":str(package),"mandarin_audio":planned["mandarin_audio"],"blocker":planned["mandarin_audio"]["reason"]}
    data.pop("pause",None); atomic_write_json(manifest_path,data)
    from .validate import validate_media_request
    checked=validate_media_request(package); return {"status":"complete" if checked["ok"] else "failed","package":str(package),"validation":checked}

def _record(data,key,path,package,provenance,started):
    data["artifacts"][key]=path.resolve().relative_to(package).as_posix(); data["provenance"][key]=provenance
    data["stages"][f"{key}_ready"]={"duration_ms":round((time.monotonic()-started)*1000),"fingerprint":fingerprint([path]),"cache_hit":False,"result":"passed"}

def _existing(package,data):
    mapping={"source_audio":"source_audio_ready","source_video":"source_video_ready","source_subtitle":"source_subtitle_ready","localized_audio":"localized_audio_ready"}
    return {mapping.get(key,key) for key,raw in data.get("artifacts",{}).items() if (package/raw).is_file()}
