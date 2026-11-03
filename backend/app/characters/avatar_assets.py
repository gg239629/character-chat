"""Approved character portraits; custom per-user avatars take precedence."""
DEFAULTS={
    'wan':'/static/avatars/wan-v1.png',
    'yan':'/static/avatars/yan-v3.png',
    'liu':'/static/avatars/liu-v6.png',
}


def display(role,custom=''):
    return custom or DEFAULTS.get(role,'')
