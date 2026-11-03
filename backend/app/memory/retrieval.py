"""Permission-first LlamaIndex vector retrieval fused with lexical ranking.

Only the already-authorized candidate set is indexed. The persistent cache stores
vectors and digests, not copies of private text. Embeddings are opt-in per user.
"""
import hashlib
import json
from dataclasses import dataclass,field
import time
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import lru_cache
from sqlalchemy import String,JSON,Float,select
from sqlalchemy.orm import Session,Mapped,mapped_column
from backend.app.infrastructure import storage
from backend.app.characters import model_defaults


class Embedding(storage.Base):
    __tablename__='chat_memory_embeddings'
    id:Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    vector:Mapped[list]=mapped_column(JSON)
    created_at:Mapped[float]=mapped_column(Float)


def embedding_config():
    snapshot=storage.request_snapshot.get()
    if snapshot is not None and '_embedding_config' in snapshot:return snapshot['_embedding_config']
    cfg=model_defaults.read(storage.scope.get()[0])
    result=cfg if cfg.get('embedding_model') and cfg.get('api_key') else None
    if snapshot is not None:snapshot['_embedding_config']=result
    return result


def build_index(records,cfg):
    """LlamaIndex owns splitting/indexing/vector retrieval; caller owns visibility."""
    from llama_index.core import VectorStoreIndex
    from llama_index.core.schema import TextNode
    from llama_index.core.embeddings import BaseEmbedding
    from pydantic import PrivateAttr
    from openai import OpenAI
    import re
    user=storage.scope.get()[0]
    engine=storage.get_store().engine
    with _lock:epoch=_epochs.get((engine,user),0)
    fingerprint=hashlib.sha256((str(cfg.get('base_url',''))+':'+cfg['embedding_model']).encode()).hexdigest()

    class CachedEmbedding(BaseEmbedding):
        _cfg:dict=PrivateAttr()
        def __init__(self):super().__init__(model_name=cfg['embedding_model'],embed_batch_size=32);self._cfg=cfg
        def _get_text_embedding(self,text):return self._get_text_embeddings([text])[0]
        def _get_text_embeddings(self,texts):
            identities=[hashlib.sha256((user+':'+fingerprint+':'+text).encode()).hexdigest() for text in texts]
            with Session(storage.get_store().engine) as db:
                found={row.id:row.vector for row in db.scalars(select(Embedding).where(Embedding.id.in_(identities),Embedding.user_id==user))}
            missing={identity:text for identity,text in zip(identities,texts) if identity not in found}
            if missing:
                from backend.app.infrastructure import model_usage
                payload=[{'role':'user','content':'\n'.join(missing.values())}]
                from backend.app.infrastructure.model_transport import client_options
                with OpenAI(api_key=self._cfg['api_key'],base_url=self._cfg.get('base_url') or None,timeout=20,max_retries=0,**client_options(self._cfg.get('base_url'))) as client:
                    with model_usage.Measure('embedding',{**self._cfg,'model':self.model_name},payload) as metric:
                        response=client.embeddings.create(model=self.model_name,input=list(missing.values()))
                        metric.observe(response)
                if len(response.data)!=len(missing):raise ValueError('Embedding batch size mismatch')
                added={identity:item.embedding for identity,item in zip(missing,response.data)}
                from sqlalchemy.exc import IntegrityError
                with Session(storage.get_store().engine) as db:
                    with _lock:current_epoch=_epochs.get((engine,user),0)
                    if current_epoch!=epoch:return [found.get(identity,added.get(identity)) for identity in identities]
                    for identity,vector in added.items():db.add(Embedding(id=identity,user_id=user,vector=vector,created_at=time.time()))
                    try:db.commit()
                    except IntegrityError:db.rollback()
                found.update(added)
            return [found[identity] for identity in identities]
        def _get_query_embedding(self,query):return self._get_text_embedding(query)
        async def _aget_query_embedding(self,query):
            import asyncio
            return await asyncio.to_thread(self._get_text_embedding,query)

    # All supplied records have already passed the caller/role visibility filter.
    # Batch embeddings once; later turns reuse digest-keyed vectors.
    candidates=range(len(records))
    nodes=[]
    for index in candidates:
        for part,chunk in enumerate(split_text(str(records[index].get('text',''))[:2100])):
            nodes.append(TextNode(id_=f'{index}:{part}',text=chunk,metadata={'candidate':index},excluded_embed_metadata_keys=['candidate']))
    return VectorStoreIndex(nodes,embed_model=CachedEmbedding(),show_progress=False) if nodes else None


@lru_cache(maxsize=256)
def split_text(text):
    from llama_index.core.node_parser import SentenceSplitter
    import re
    splitter=SentenceSplitter(chunk_size=700,chunk_overlap=60,tokenizer=list,
        chunking_tokenizer_fn=lambda value:re.findall(r'[^。！？.!?]+[。！？.!?]?',value) or [value])
    return tuple(splitter.split_text(text))


_lock=threading.RLock()
_indexes=OrderedDict()
_pending={}
_failures={}
_epochs={}
_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='memory-index')


def index_key(records,cfg):
    engine=storage.get_store().engine
    digest=hashlib.sha256(json.dumps([str(r.get('text',''))[:2100] for r in records],ensure_ascii=False).encode()).hexdigest()
    return (engine,*storage.scope.get(),cfg.get('base_url',''),cfg['embedding_model'],digest)


@dataclass
class IndexEntry:
    index:object
    key:tuple
    fingerprints:tuple
    lock:object=field(default_factory=threading.RLock)


def prepare_index(key,records,cfg):
    fingerprints=tuple(hashlib.sha256(str(r.get('text',''))[:2100].encode()).hexdigest() for r in records)
    with _lock:
        epoch=_epochs.get(key[:2],0)
        parents=[entry for old,entry in _indexes.items() if old[:-1]==key[:-1] and
            len(entry.fingerprints)<len(fingerprints) and fingerprints[:len(entry.fingerprints)]==entry.fingerprints]
        parent=max(parents,key=lambda entry:len(entry.fingerprints),default=None)
        if parent:_indexes.pop(parent.key,None)
    try:
        if parent:
            # Ordinary appended memories only split/embed/insert the new nodes.
            # Remove the old key before mutation; readers verify the exact corpus under its lock.
            with parent.lock:
                from llama_index.core.schema import TextNode
                nodes=[]
                for index in range(len(parent.fingerprints),len(records)):
                    for part,chunk in enumerate(split_text(str(records[index].get('text',''))[:2100])):
                        nodes.append(TextNode(id_=f'{index}:{part}',text=chunk,metadata={'candidate':index},excluded_embed_metadata_keys=['candidate']))
                parent.key=key
                parent.index.insert_nodes(nodes)
                parent.fingerprints=fingerprints
                entry=parent
        else:entry=IndexEntry(build_index(records,cfg),key,fingerprints)
        with _lock:
            if epoch!=_epochs.get(key[:2],0):return None
            _indexes[key]=entry;_indexes.move_to_end(key)
            while len(_indexes)>16:_indexes.popitem(last=False)
        return entry
    except Exception as error:
        with _lock:_failures[key]=time.monotonic()+60
        from backend.app.infrastructure import task_health
        from backend.app.core.context import chat_world
        world=chat_world.get()
        task_health.record(storage.scope.get()[0],world['id'] if world else storage.scope.get()[1],'embedding','retrieval',error)
        return None


def clear_indexes(user=None):
    with _lock:
        affected={key[:2] for key in list(_indexes)+list(_pending) if user is None or key[1]==user}
        for owner in affected:_epochs[owner]=_epochs.get(owner,0)+1
        for key in list(_indexes):
            if user is None or key[1]==user:_indexes.pop(key,None)
        for key in list(_failures):
            if user is None or key[1]==user:_failures.pop(key,None)
    split_text.cache_clear()


def wait_pending():
    with _lock:tasks=list(_pending.values())
    for task in tasks:
        try:task.result()
        except Exception:pass


def hybrid_rank(query,records,lexical,cfg):
    if not query:return lexical
    key=index_key(records,cfg)
    with _lock:
        entry=_indexes.get(key)
        if key in _failures and _failures[key]>time.monotonic():return lexical
        if entry is None and os.getenv('RUN_MAINTENANCE_WORKER','true').lower() not in ('false','0','no'):
            if key not in _pending and len(_pending)<8:
                context=copy_context()
                task=_executor.submit(context.run,prepare_index,key,[dict(r) for r in records],dict(cfg))
                _pending[key]=task
                def completed(_):
                    with _lock:_pending.pop(key,None)
                task.add_done_callback(completed)
            return lexical
    if entry is None:entry=prepare_index(key,records,cfg)
    if entry is None or entry.index is None:return lexical
    if not entry.lock.acquire(blocking=False):return lexical
    try:
        if entry.key!=key:return lexical
        matches=entry.index.as_retriever(similarity_top_k=12).retrieve(query[:1500])
    finally:entry.lock.release()
    semantic=[]
    for match in matches:
        candidate=match.node.metadata['candidate']
        if (match.score or 0)>=.72 and candidate not in semantic:semantic.append(candidate)
    scores={};coverage={i:c for c,_,i in lexical}
    for ranking in ([i for _,_,i in lexical],semantic):
        for position,candidate in enumerate(ranking):scores[candidate]=scores.get(candidate,0)+1/(60+position)
    return [(max(coverage.get(i,0),.3 if i in semantic else 0),score,i) for i,score in sorted(scores.items(),key=lambda pair:pair[1],reverse=True)]


def rank(query,records,lexical):
    cfg=embedding_config()
    if not cfg or not records:return lexical
    try:return hybrid_rank(query,records,lexical,cfg)
    except Exception as error:
        # Failure is observable without retaining queries, provider URLs or keys.
        from backend.app.infrastructure import task_health
        from backend.app.core.context import chat_world
        world=chat_world.get()
        task_health.record(storage.scope.get()[0],world['id'] if world else storage.scope.get()[1],'embedding','retrieval',error)
        return lexical
