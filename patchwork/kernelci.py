# SPDX-License-Identifier: GPL-2.0-or-later

"""KernelCI configuration shared by the optional worker and web views.

No kci-dev imports or network requests belong in the web process.
"""

from copy import deepcopy
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def targets_for(project):
    return getattr(settings, 'KERNELCI_PROJECTS', {}).get(project.linkname, {})


def can_test(user, series):
    return bool(
        user.is_authenticated
        and user.is_active
        and series
        and series.project
        and targets_for(series.project)
    )


def target_selection(project, name):
    target = targets_for(project)[name]
    selection = {
        'label': target.get('label', name),
        'git_url': target['git_url'],
        'branch': target['branch'],
        'checkout': target.get('checkout', ''),
        'jobs': target['jobs'],
        'platforms': target.get('platforms'),
        'api': settings.KERNELCI_API_URL,
        'pipeline': settings.KERNELCI_PIPELINE_URL,
    }
    for key in ('api', 'pipeline'):
        value = selection[key]
        url = urlsplit(value)
        if (
            url.scheme != 'https'
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ImproperlyConfigured('KernelCI requires HTTPS endpoints')
        selection[key] = value.rstrip('/')
    for key in ('jobs', 'platforms'):
        values = selection[key]
        if key == 'platforms' and values is None:
            continue
        if (
            not isinstance(values, list)
            or (key == 'jobs' and not values)
            or any(not isinstance(v, str) or not v.strip() for v in values)
            or len(set(values)) != len(values)
        ):
            raise ImproperlyConfigured('Invalid KernelCI job/platform filters')
    if selection['checkout'] and not re.fullmatch(
        r'[0-9a-f]{24}', selection['checkout']
    ):
        raise ImproperlyConfigured('Invalid KernelCI checkout ID')
    if not selection['git_url'] or not selection['branch']:
        raise ImproperlyConfigured('KernelCI requires a Git URL and branch')
    return deepcopy(selection)
