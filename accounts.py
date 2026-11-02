"""Optional local accounts with hashed passwords and revocable opaque sessions."""
import hashlib
import hmac
import secrets
import time
import os
from sqlalchemy import String, Float, select, delete
from sqlalchemy.orm import Mapped,mapped_column,Session
import storage


class Account(storage.Base):
    __tablename__='chat_accounts'
    name:Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),unique=True)
    password_hash:Mapped[str]=mapped_column(String(256))


class LoginSession(storage.Base):
    __tablename__='chat_login_sessions'
    token_hash:Mapped[str]=mapped_column(String(64),primary_key=True)
    user_id:Mapped[str]=mapped_column(String(128),index=True)
    expires:Mapped[float]=mapped_column(Float)


_runtime_policy = {}


class AccountPolicy(storage.Base):
    __tablename__='chat_account_policy'
    id:Mapped[str]=mapped_column(String(32),primary_key=True)
    enabled:Mapped[bool]=mapped_column(default=False)
    mode:Mapped[str]=mapped_column(String(16),default='invite')
    invite_digest:Mapped[str]=mapped_column(String(64),default='')


def load_policy():
    """Load installation policy once at startup; environment settings take precedence."""
    global _runtime_policy
    with Session(storage.get_store().engine) as db:
        row=db.get(AccountPolicy,'default')
        _runtime_policy={'enabled':row.enabled,'mode':row.mode,'invite_digest':row.invite_digest} if row else {}


def configure_policy(invite_code):
    if len(invite_code)<16:raise ValueError('邀请码至少16字符')
    with Session(storage.get_store().engine) as db:
        if not db.scalar(select(Account).where(Account.user_id=='owner')):
            raise ValueError('请先绑定管理员，再启用登录')
        row=db.get(AccountPolicy,'default')
        if not row:row=AccountPolicy(id='default');db.add(row)
        row.enabled=True;row.mode='invite';row.invite_digest=hashlib.sha256(invite_code.encode()).hexdigest()
        db.commit()


def enabled():
    value=os.getenv('ENABLE_USER_ACCOUNTS')
    return value.lower() in ('1','true','yes') if value is not None else bool(_runtime_policy.get('enabled',False))


def password_hash(password,salt):
    return hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600000).hex()


def register(name,password):
    import re
    name=name.strip().lower()
    if not re.fullmatch(r'[a-z0-9_\-]{3,40}',name) or not 10<=len(password)<=128:
        raise ValueError('用户名需为3至40位字母、数字、下划线或短横线，密码需为10至128位')
    salt=secrets.token_hex(16);user='u_'+secrets.token_hex(16)
    from sqlalchemy.exc import IntegrityError
    with Session(storage.get_store().engine) as db:
        db.add(Account(name=name,user_id=user,password_hash=salt+':'+password_hash(password,salt)))
        db.add(storage.User(id=user))
        db.add(storage.Conversation(id=root_id(user),user_id=user,data={'title':'大家一起','recent_log':[],
            'initiative_enabled':False,'story_clock':{'day':1,'minute':480},'scene_state':{'时间':'第一天早晨08:00',
                **{name:'卧室·刚起床' for name in ('小婉','小研','小柳')}}},version=0))
        try:db.flush();db.commit()
        except IntegrityError:raise ValueError('用户名已被使用')
    return user


def root_id(user):return 'default' if user=='owner' else hashlib.sha256(('account-story:'+user).encode()).hexdigest()[:48]


def registration_mode():
    value=os.getenv('REGISTRATION_MODE',_runtime_policy.get('mode','invite')).lower()
    return value if value in ('invite','open','closed') else 'closed'


def check_invite(code):
    mode=registration_mode()
    expected=os.getenv('REGISTRATION_INVITE_CODE')
    if mode=='closed':raise ValueError('暂未开放注册')
    if mode=='invite':
        valid=(len(expected)>=16 and hmac.compare_digest(code.encode(),expected.encode())) if expected is not None else (
            bool(_runtime_policy.get('invite_digest')) and hmac.compare_digest(hashlib.sha256(code.encode()).hexdigest(),_runtime_policy['invite_digest']))
        if not valid:raise ValueError('邀请码无效，请向网站管理员获取')


def account_name(user):
    with Session(storage.get_store().engine) as db:
        return db.scalar(select(Account.name).where(Account.user_id==user))


def change_password(user,current,password,endpoint=None):
    if not 10<=len(password)<=128:raise ValueError('密码需为10至128位')
    with Session(storage.get_store().engine) as db:
        row=db.scalar(select(Account).where(Account.user_id==user).with_for_update())
        if not row:raise ValueError('账号不存在')
        salt,digest=row.password_hash.split(':')
        if current is not None and not hmac.compare_digest(password_hash(current,salt),digest):
            raise ValueError('当前密码不正确')
        salt=secrets.token_hex(16)
        row.password_hash=salt+':'+password_hash(password,salt)
        db.execute(delete(LoginSession).where(LoginSession.user_id==user))
        from push_notifications import Subscription
        db.execute(delete(Subscription).where(Subscription.user_id==user))
        db.commit()


def create_owner(name,password):
    """Explicit operator-only binding; never give the first registrant owner data."""
    import re
    name=name.strip().lower()
    if not re.fullmatch(r'[a-z0-9_\-]{3,40}',name) or not 10<=len(password)<=128:
        raise ValueError('用户名需为3至40位字母、数字、下划线或短横线，密码需为10至128位')
    salt=secrets.token_hex(16)
    from sqlalchemy.exc import IntegrityError
    with Session(storage.get_store().engine) as db:
        if db.scalar(select(Account).where(Account.user_id=='owner')):
            raise ValueError('已有管理员绑定原有数据，请使用 reset-password')
        db.add(Account(name=name,user_id='owner',password_hash=salt+':'+password_hash(password,salt)))
        try:db.commit()
        except IntegrityError:raise ValueError('用户名已被使用')


class AuthLimit(storage.Base):
    __tablename__='chat_auth_limits'
    identity:Mapped[str]=mapped_column(String(64),primary_key=True)
    until:Mapped[float]=mapped_column(Float)
    count:Mapped[int]=mapped_column(default=0)


def allow_attempt(identity,limit=12,period=60):
    """Atomic shared limits, including across server processes; store only a digest."""
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError
    key=hashlib.sha256(identity.encode()).hexdigest();now=time.time()
    with Session(storage.get_store().engine) as db:
        db.execute(delete(AuthLimit).where(AuthLimit.until<now))
        result=db.execute(update(AuthLimit).where(AuthLimit.identity==key,AuthLimit.count<limit).values(count=AuthLimit.count+1))
        if result.rowcount:db.commit();return True
        if db.get(AuthLimit,key):db.rollback();return False
        db.add(AuthLimit(identity=key,until=now+period,count=1))
        try:db.commit();return True
        except IntegrityError:db.rollback();return allow_attempt(identity,limit,period)


def login(name,password,previous_token='',endpoint=''):
    with Session(storage.get_store().engine) as db:
        row=db.scalar(select(Account).where(Account.name==name.strip().lower()).with_for_update())
        salt,digest=row.password_hash.split(':') if row else ('0'*32,'0'*64)
        valid=hmac.compare_digest(password_hash(password,salt),digest)
        if not row or not valid:raise ValueError('用户名或密码错误')
        previous=db.get(LoginSession,hashlib.sha256(previous_token.encode()).hexdigest()) if previous_token else None
        if previous:
            if endpoint:
                from push_notifications import Subscription
                db.execute(delete(Subscription).where(Subscription.user_id==previous.user_id,
                    Subscription.id==hashlib.sha256(endpoint.encode()).hexdigest()))
            db.delete(previous)
        token=secrets.token_urlsafe(32)
        db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(),user_id=row.user_id,expires=time.time()+7*86400))
        db.execute(delete(LoginSession).where(LoginSession.expires<time.time()));db.commit()
        return token


def identify(token):
    if not token:return None
    with Session(storage.get_store().engine) as db:
        row=db.get(LoginSession,hashlib.sha256(token.encode()).hexdigest())
        return row.user_id if row and row.expires>time.time() else None


def logout(token,endpoint=None):
    with Session(storage.get_store().engine) as db:
        row=db.get(LoginSession,hashlib.sha256(token.encode()).hexdigest())
        if row and endpoint:
            from push_notifications import Subscription
            db.execute(delete(Subscription).where(Subscription.user_id==row.user_id,
                Subscription.id==hashlib.sha256(endpoint.encode()).hexdigest()))
        db.execute(delete(LoginSession).where(LoginSession.token_hash==hashlib.sha256(token.encode()).hexdigest()));db.commit()
