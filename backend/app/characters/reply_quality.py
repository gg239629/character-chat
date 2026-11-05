"""Stop repeated dialogue without an extra model call or a fixed speaker count."""
import re
from difflib import SequenceMatcher


def normalized(text):
    text=re.sub(r'^\s*小[婉研柳][：:]','',text)
    return re.sub(r'[\s，。！？、,.!?…—～~；;：:（）()]','',text)


def redundant(text,previous):
    body=normalized(text)
    if not body:return True
    if len(previous)>=2 and body in ('嗯','嗯嗯','好','好的','好吧','行','知道了','嗯好','就这样','没停','还在','我没停'):return True
    for earlier in previous[-8:]:
        old=normalized(earlier)
        if body==old:return True
        if len(body)>=16 and len(old)>=16:
            # Different numbers, questions and named recipients may carry new content.
            if re.findall(r'\d+|小婉|小研|小柳|[?？]',text)!=re.findall(r'\d+|小婉|小研|小柳|[?？]',earlier):continue
            if SequenceMatcher(None,body,old,autojunk=False).ratio()>=.86:return True
    return False


def trim_repeated(text,history):
    """Remove same-speaker repeated clauses; keep new actions, questions and facts."""
    match=re.match(r'^\s*(小[婉研柳])[：:]',text)
    if not match:return text
    name=match[1]
    start=next((i+1 for i in range(len(history)-1,-1,-1) if history[i].get('role')=='user'),0)
    earlier=[m['content'] for m in history[max(start,len(history)-20):] if m.get('role')=='assistant' and m.get('content','').startswith((name+'：',name+':'))][-4:]
    if not earlier:return text
    def clauses(body):
        return [c.strip() for c in re.split(r'(?<=[。！？!?])|\n+',re.sub(r'[（(][^（）()]*[）)]','',body)) if c.strip()]
    canonical=lambda s:re.sub(r'[呢呀啊啦]$','',normalized(s))
    old=[canonical(c) for e in earlier for c in clauses(re.sub(r'^小[婉研柳][：:]','',e))]
    body=text[match.end():];kept=[];removed=False
    # Action paragraphs are retained intact: repetition of speech does not undo an action.
    for part in re.split(r'([（(][^（）()]*[）)])',body):
        if part.startswith(('（','(')):kept.append(part);continue
        for clause in re.split(r'(?<=[。！？!?])|\n+',part):
            value=canonical(clause)
            if not value:continue
            if '?' in clause or '？' in clause:kept.append(clause);continue
            def same(previous):
                # Preserve changed quantities, names and negation.
                signature=lambda s:re.findall(r'\d+|[一二三四五六七八九十]+|小婉|小研|小柳|没|不|已经|还',s)
                return signature(value)==signature(previous) and (value==previous or
                    min(len(value),len(previous))>=5 and SequenceMatcher(None,value,previous,autojunk=False).ratio()>=.8)
            if not any(same(p) for p in old):kept.append(clause)
            else:removed=True
    if not removed:return text
    result='\n'.join(p.strip() for p in kept if p.strip()).strip()
    return name+'：'+result if result else ''


def repeated_initiative(text,speaker,history):
    """Block re-answering the same user turn, including cosmetic action changes."""
    start=next((i+1 for i in range(len(history)-1,-1,-1) if history[i].get('role')=='user'),0)
    def speech(value):
        return re.sub(r'[（(][^（）()]*[）)]','',value)
    for message in history[start:]:
        if message.get('role')!='assistant' or message.get('speaker')!=speaker:continue
        earlier=message['content']
        if normalized(text)==normalized(earlier):return True
        current,old=speech(text),speech(earlier)
        if len(normalized(current))>=16 and len(normalized(old))>=16 and redundant(current,[old]):return True
    return False
