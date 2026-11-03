"""Headless browser fixture. Binds only loopback, never production databases/models."""
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
root=Path(__file__).resolve().parents[1]
folder=root/'.test-tmp';folder.mkdir(exist_ok=True)
from uuid import uuid4
os.environ.update(DATABASE_URL='sqlite:///'+(folder/('browser-'+uuid4().hex+'.db')).as_posix(),ENABLE_USER_ACCOUNTS='true',REGISTRATION_MODE='open',
    RUN_ROLE_SCHEDULER='false',RUN_MAINTENANCE_WORKER='false',OPENBLAS_NUM_THREADS='1')
from cryptography.fernet import Fernet
os.environ['APP_ENCRYPTION_KEY']=Fernet.generate_key().decode()
import server
server.load_config=lambda:{'api_key':'fixture','model':'fixture','base_url':'https://fixture.invalid/v1','temperature':1,'max_tokens':1500}
from backend.app.infrastructure import storage
from backend.app.accounts import accounts
storage.initialize();accounts.create_owner('browser_admin','browser-test-password-123')
app=server.app


@app.post('/__test__/image')
def seed_image():
    import base64
    from types import SimpleNamespace
    from unittest.mock import MagicMock,patch
    from backend.app.characters import image_generation as images
    images.save_settings('owner',dict(enabled=True,inherit_connection=False,model='gpt-image-2.5',base_url='https://fixture.invalid/v1',api_key='fixture'))
    job=images.enqueue('owner','default','liu','liu','一张清晨阳台的自拍','1024x1024',uuid4().hex)
    data=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII=')
    sample=root/'.test-tmp/gateway-image.png'
    if sample.is_file():data=sample.read_bytes()
    fake=MagicMock();fake.__enter__.return_value=fake
    fake.images.generate.return_value=SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(data).decode())],usage=None)
    with patch('openai.OpenAI',return_value=fake):images.process(job['id'])
    return images.get('owner','default','liu',job['id'])


@app.post('/__test__/message')
def seed_message():
    from backend.app.conversations import message_service
    from backend.app.runtime import role_actions
    from uuid import uuid4
    with role_actions.window('owner','default','group'):
        message_service.durable_append(uuid4().hex,0,'assistant','小研：今天一起去植物园吧。','小研')
    return {'ok':True}


if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8799)
