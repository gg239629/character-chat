"""Operator commands. Passwords are entered privately, never command-line arguments."""
import argparse
import getpass
import secrets
import accounts
import storage


def main():
    parser=argparse.ArgumentParser(description='账号管理（在连接目标数据库的环境中执行）')
    parser.add_argument('command',choices=['create-owner','reset-password','invite-code'])
    parser.add_argument('username',nargs='?')
    args=parser.parse_args()
    if args.command=='invite-code':
        print(secrets.token_urlsafe(24));return
    if not args.username:parser.error('请填写用户名')
    password=getpass.getpass('新密码（至少10位）: ')
    if password!=getpass.getpass('再次输入: '):parser.error('两次密码不一致')
    # Import all mapped models before creating an empty database.
    import server
    storage.initialize()
    try:
        if args.command=='create-owner':accounts.create_owner(args.username,password)
        else:
            from sqlalchemy.orm import Session
            with Session(storage.get_store().engine) as db:
                account=db.get(accounts.Account,args.username.strip().lower())
                if not account:raise ValueError('账号不存在')
                user=account.user_id
            accounts.change_password(user,None,password)
    except ValueError as error:parser.error(str(error))
    print('已完成。原有聊天和模型配置保持不变。')


if __name__=='__main__':main()
