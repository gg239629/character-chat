"""Generate reusable deployment values into an ignored file; never print secrets."""
import os
from pathlib import Path
import secrets
from backend.app.infrastructure import secrets_store
from backend.app.conversations import push_notifications


def main():
    root=Path(__file__).resolve().parent/'data'
    root.mkdir(exist_ok=True)
    path=root/'background.env'
    if path.exists():
        print('已存在配置，保留原有密钥：',path)
        return
    # Match the key already used by the local encrypted database.
    cipher=secrets_store.cipher()
    key=os.getenv('APP_ENCRYPTION_KEY') or secrets_store.KEY_FILE.read_text().strip()
    pair=push_notifications.keys()
    if not pair:
        raise RuntimeError('请先配置 VAPID_PRIVATE_KEY')
    values={'APP_ENCRYPTION_KEY':key,'VAPID_PRIVATE_KEY':pair[0],
        'ROLE_CRON_TOKEN':os.getenv('ROLE_CRON_TOKEN') or secrets.token_urlsafe(40),
        'VAPID_SUBJECT':os.getenv('VAPID_SUBJECT','mailto:admin@example.com'),'RUN_ROLE_SCHEDULER':'true'}
    with path.open('x',encoding='utf-8') as file:
        file.write('\n'.join(f'{name}={value}' for name,value in values.items())+'\n')
    try:
        path.chmod(0o600)
    except OSError:
        pass
    print('部署配置已生成，请在本机打开后复制到 Render 环境变量：',path)
    print('此文件包含密钥，不要提交、分享或覆盖现有云端加密密钥。')


if __name__=='__main__':
    main()
