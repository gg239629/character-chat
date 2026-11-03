"""Revocable, scoped personal MCP credentials. Store digests, never bearer tokens."""
import hashlib
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass
from sqlalchemy import String, JSON, Float, Integer, select, update, delete
from sqlalchemy.orm import Mapped,mapped_column,Session
from backend.app.infrastructure import storage

principal=ContextVar('mcp_principal',default=None)
SCOPES={'read','chat','wan','yan','liu','group'}


class Credential(storage.Base):
    __tablename__='chat_mcp_credentials'
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    label:Mapped[str]=mapped_column(String(80))
    digest:Mapped[str]=mapped_column(String(64),unique=True)
    scopes:Mapped[list]=mapped_column(JSON)
    expires_at:Mapped[float]=mapped_column(Float)
    revoked:Mapped[int]=mapped_column(Integer,default=0)
    day:Mapped[int]=mapped_column(Integer,default=0)
    calls:Mapped[int]=mapped_column(Integer,default=0)
    daily_limit:Mapped[int]=mapped_column(Integer,default=50)


class Charge(storage.Base):
    __tablename__='chat_mcp_charges'
    id:Mapped[str]=mapped_column(String(64),primary_key=True)
    credential_id:Mapped[str]=mapped_column(String(32),index=True)


class TokenHold(storage.Base):
    __tablename__='chat_mcp_token_holds'
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    credential_id:Mapped[str]=mapped_column(String(32),index=True)
    amount:Mapped[int]=mapped_column(Integer)
    expires_at:Mapped[float]=mapped_column(Float,index=True)


class TokenBudget(storage.Base):
    __tablename__='chat_mcp_token_budgets'
    credential_id:Mapped[str]=mapped_column(String(32),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    daily_tokens:Mapped[int]=mapped_column(Integer,default=0)
    day:Mapped[int]=mapped_column(Integer,default=0)
    used:Mapped[int]=mapped_column(Integer,default=0)
    reserved:Mapped[int]=mapped_column(Integer,default=0)
    estimated_calls:Mapped[int]=mapped_column(Integer,default=0)


@dataclass(frozen=True)
class Principal:
    user_id:str
    client_id:str
    scopes:tuple[str,...]

    @property
    def world(self):return hashlib.sha256(('mcp:'+self.user_id+':'+self.client_id).encode()).hexdigest()[:48]

    def require(self,operation,channel=None):
        if operation not in self.scopes or channel and channel not in self.scopes:
            raise PermissionError('MCP credential does not permit this operation')


def create(user,label,scopes,daily_limit=50,days=30,daily_tokens=0):
    if not label.strip() or len(label)>80:raise ValueError('请输入1至80字的名称')
    if not set(scopes)<=SCOPES or not scopes:raise ValueError('无效权限')
    if not 1<=daily_limit<=1000 or not 1<=days<=365:raise ValueError('额度或有效期超出范围')
    if not 0<=daily_tokens<=10000000:raise ValueError('Token 预算超出范围')
    token='ccm_'+secrets.token_urlsafe(32);identity=secrets.token_hex(16)
    with Session(storage.get_store().engine) as db:
        if len(list(db.scalars(select(Credential.id).where(Credential.user_id==user,Credential.revoked==0,Credential.expires_at>time.time()))))>=20:
            raise ValueError('请先撤销不用的密钥（最多20个）')
        db.add(Credential(id=identity,user_id=user,label=label.strip(),digest=hashlib.sha256(token.encode()).hexdigest(),
            scopes=sorted(set(scopes)),expires_at=time.time()+days*86400,daily_limit=daily_limit,day=0,calls=0,revoked=0))
        db.add(TokenBudget(credential_id=identity,user_id=user,daily_tokens=daily_tokens,day=0,used=0,reserved=0,estimated_calls=0));db.commit()
    return {'id':identity,'token':token}


def identify(token):
    if not token or len(token)>256:return None
    with Session(storage.get_store().engine) as db:
        row=db.scalar(select(Credential).where(Credential.digest==hashlib.sha256(token.encode()).hexdigest(),Credential.revoked==0,Credential.expires_at>time.time()))
        return Principal(row.user_id,row.id,tuple(row.scopes)) if row else None


def list_keys(user):
    with Session(storage.get_store().engine) as db:
        budgets={b.credential_id:b for b in db.scalars(select(TokenBudget).where(TokenBudget.user_id==user))}
        result=[];day=int(time.time()//86400)
        for row in db.scalars(select(Credential).where(Credential.user_id==user)):
            item={key:getattr(row,key) for key in ('id','label','scopes','expires_at','revoked','daily_limit')}
            item['calls']=row.calls if row.day==day else 0
            budget=budgets.get(row.id)
            item.update(daily_tokens=budget.daily_tokens if budget else 0,used_tokens=budget.used if budget and budget.day==day else 0,
                reserved_tokens=budget.reserved if budget and budget.day==day else 0,estimated_calls=budget.estimated_calls if budget and budget.day==day else 0)
            result.append(item)
        return result


def revoke(user,identity):
    with Session(storage.get_store().engine) as db:
        result=db.execute(update(Credential).where(Credential.id==identity,Credential.user_id==user).values(revoked=1));db.commit()
        if not result.rowcount:raise PermissionError('Credential unavailable')


def reserve(actor,request_id):
    now=time.time();day=int(now//86400)
    with Session(storage.get_store().engine) as db:
        if db.get(Charge,request_id):return
        db.execute(update(Credential).where(Credential.id==actor.client_id,Credential.day!=day).values(day=day,calls=0))
        result=db.execute(update(Credential).where(Credential.id==actor.client_id,Credential.user_id==actor.user_id,
            Credential.revoked==0,Credential.expires_at>now,Credential.calls<Credential.daily_limit).values(calls=Credential.calls+1))
        if not result.rowcount:
            db.rollback()
            if db.get(Charge,request_id):return
            raise PermissionError('MCP credential expired, revoked, or daily request budget exhausted')
        db.add(Charge(id=request_id,credential_id=actor.client_id))
        from sqlalchemy.exc import IntegrityError
        try:db.commit()
        except IntegrityError:
            db.rollback()
            if not db.get(Charge,request_id):raise


class BudgetExceeded(RuntimeError):pass


def budget_client():
    from backend.app.core.context import chat_world
    world=chat_world.get()
    if not world or world.get('data',{}).get('origin')!='mcp':return None
    return world.get('data',{}).get('mcp_client_id')


def begin_model(messages,output_limit=2048):
    client=budget_client()
    if not client:return None
    import json
    # Conservative admission estimate; providers may omit actual usage.
    amount=len(json.dumps(messages,ensure_ascii=False).encode('utf-8'))+output_limit
    engine=storage.get_store().engine;user=storage.scope.get()[0];day=int(time.time()//86400)
    with Session(engine) as db:
        credential=db.get(Credential,client)
        if not credential or credential.user_id!=user or credential.revoked or credential.expires_at<=time.time():raise BudgetExceeded('MCP credential unavailable')
        budget=db.get(TokenBudget,client)
        if not budget:
            # Existing credentials remain unlimited until the owner replaces them with a budgeted key.
            return None
        db.execute(update(TokenBudget).where(TokenBudget.credential_id==client,TokenBudget.day!=day).values(day=day,used=0,estimated_calls=0))
        expired=list(db.execute(delete(TokenHold).where(TokenHold.credential_id==client,TokenHold.expires_at<=time.time()).returning(TokenHold.amount)).scalars())
        if expired:
            db.execute(update(TokenBudget).where(TokenBudget.credential_id==client).values(
                used=TokenBudget.used+sum(expired),reserved=TokenBudget.reserved-sum(expired),estimated_calls=TokenBudget.estimated_calls+len(expired)))
        condition=(TokenBudget.daily_tokens==0)|(TokenBudget.used+TokenBudget.reserved+amount<=TokenBudget.daily_tokens)
        result=db.execute(update(TokenBudget).where(TokenBudget.credential_id==client,condition).values(reserved=TokenBudget.reserved+amount))
        if not result.rowcount:raise BudgetExceeded('MCP token budget exhausted or insufficient for this call')
        hold=secrets.token_hex(16)
        db.add(TokenHold(id=hold,credential_id=client,amount=amount,expires_at=time.time()+900))
        db.commit()
    return engine,client,amount,hold


def end_model(reservation,input_tokens,output_tokens):
    if not reservation:return
    engine,client,amount,hold=reservation
    known=input_tokens is not None and output_tokens is not None
    used=input_tokens+output_tokens if known else amount
    with Session(engine) as db:
        removed=db.execute(delete(TokenHold).where(TokenHold.id==hold).returning(TokenHold.id)).scalar()
        if removed is None:return
        # Preserve outstanding reservations across a UTC day boundary.
        db.execute(update(TokenBudget).where(TokenBudget.credential_id==client).values(
            reserved=TokenBudget.reserved-amount,used=TokenBudget.used+used,
            estimated_calls=TokenBudget.estimated_calls+(0 if known else 1)))
        db.commit()
