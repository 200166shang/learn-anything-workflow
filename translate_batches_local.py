import json, sys, urllib.request
from pathlib import Path

package = Path(sys.argv[1])
batches = json.loads((package/'translation/batches.json').read_text())['batches']
out = package/'translation/localized_script.json'
segments = []
for batch in batches:
  for offset in range(0, len(batch['segments']), 24):
    chunk = batch['segments'][offset:offset+24]
    payload = {
        'model': 'qwen2.5:7b', 'stream': False, 'format': 'json',
        'options': {'temperature': 0.15},
        'prompt': ('将下面 JSON 中每个 source_text 翻译成自然、完整、适合普通话播客朗读的简体中文。'
                   '只返回 JSON 数组，每项必须包含原 id、start、end 和 text；不要删减、总结、合并或改变顺序。'
                   '保留人名、数字、术语和不确定性。\n' + json.dumps(chunk, ensure_ascii=False))
    }
    req = urllib.request.Request('http://127.0.0.1:11434/api/generate', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req, timeout=900) as r:
        result = json.loads(json.load(r)['response'])
    if isinstance(result, dict) and isinstance(result.get('segments'), list): result = result['segments']
    if isinstance(result, dict) and all(k in result for k in ('id','start','end','text')): result = [result]
    if not isinstance(result, list): raise RuntimeError(f'bad response for {batch["id"]}: {type(result).__name__}')
    segments.extend(result)
(package/'translation').mkdir(parents=True, exist_ok=True)
(out).write_text(json.dumps({'schema':'localized-script-v1','segments':segments}, ensure_ascii=False, indent=2), encoding='utf-8')
print(out)
