"""Encrypt database API credentials with a deployment-owned Fernet key."""
import os
from pathlib import Path
from cryptography.fernet import Fernet

KEY_FILE = Path(__file__).resolve().parent / 'data' / 'credentials.key'
PREFIX = 'fernet:v1:'


def cipher():
    key = os.getenv('APP_ENCRYPTION_KEY', '').strip()
    if not key:
        if os.getenv('RENDER'):
            raise ValueError('请在 Render 配置 APP_ENCRYPTION_KEY，避免重部署后无法解密模型密钥')
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            with KEY_FILE.open('xb') as file:
                file.write(Fernet.generate_key())
            try:
                KEY_FILE.chmod(0o600)
            except OSError:
                pass
        except FileExistsError:
            pass
        key = KEY_FILE.read_bytes()
    return Fernet(key)


def encode_settings(settings):
    result = dict(settings)
    key = result.get('api_key', '')
    if key and not key.startswith(PREFIX):
        result['api_key'] = PREFIX + cipher().encrypt(key.encode()).decode()
    return result


def decode_settings(settings):
    result = dict(settings)
    key = result.get('api_key', '')
    if key.startswith(PREFIX):
        result['api_key'] = cipher().decrypt(key[len(PREFIX):].encode()).decode()
    return result


def migrate_credentials():
    """Encrypt legacy database keys when a stable deployment key is available."""
    if os.getenv('RENDER') and not os.getenv('APP_ENCRYPTION_KEY'):
        return
    import storage
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    store=storage.get_store()
    with Session(store.engine) as db:
        candidates=[(row.user_id,row.id) for row in db.scalars(select(storage.Conversation))
            if row.data.get('settings',{}).get('api_key') and not row.data['settings']['api_key'].startswith(PREFIX)]
    for user, identity in candidates:
        def change(data):
            data['settings']=encode_settings(data.get('settings',{}))
        store.mutate(user,identity,change)
