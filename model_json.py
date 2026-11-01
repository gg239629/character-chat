"""Strict JSON object decoding; tolerate wrappers, never invent missing decisions."""
import json
import re

class ModelOutputTruncated(ValueError):pass
class EmptyModelResponse(ValueError):pass

def decode(raw):
    raw=re.sub(r'<think>.*?</think>','',raw,flags=re.S).strip()
    if not raw:raise EmptyModelResponse('Model returned no content')
    if raw.startswith('```'):raw=raw.split('\n',1)[-1].rsplit('```',1)[0].strip()
    try:value=json.loads(raw)
    except json.JSONDecodeError:
        start=raw.find('{')
        if start<0:raise
        value,_=json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(value,dict):raise ValueError('Expected a JSON object')
    return value
