"""Story-level permission for unsolicited model work, shared by every worker."""
import storage


def enabled(user,world):
    return storage.get_store().read(user,world).get('initiative_enabled',True) is not False


def set_enabled(user,world,value):
    storage.get_store().mutate(user,world,lambda data:data.update(initiative_enabled=bool(value)))
    return bool(value)
