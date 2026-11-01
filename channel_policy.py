"""Shared channel participants and visibility, checked before storage adapters."""
from dataclasses import dataclass
import actors
import storage


@dataclass(frozen=True)
class Policy:
    kind:str
    participants:tuple
    user_visible:bool


def resolve(user,world,channel,actor=None):
    root=storage.get_store().read(user,world)
    if root.get('world_id'):raise PermissionError('Expected root story')
    if channel=='group':policy=Policy('group',('user',*actors.ROLES),True)
    elif channel in actors.ROLES:policy=Policy('private',('user',channel),True)
    elif channel.startswith('peer:'):
        pair=channel[5:].split('-')
        if len(pair)!=2 or len(set(pair))!=2 or any(role not in actors.ROLES for role in pair):
            raise ValueError('Invalid peer channel')
        policy=Policy('peer',tuple(sorted(pair)),False)
    else:raise ValueError('Invalid channel')
    if actor is not None and actor not in policy.participants:raise PermissionError('Actor is not a participant')
    return policy
