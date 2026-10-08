# SPDX-License-Identifier: GPL-2.0-or-later

"""Optional kci-patchwork adapter. Imported only by the worker command."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone as datetime_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from kcidev import KernelCIClient
from kci_patchwork.patchwork import Patchwork
from kci_patchwork.results import collect
from kci_patchwork.storage import WorkflowError
from kci_patchwork.workflow import checkout_snapshot
from kci_patchwork.workflow import check_patchset
from kci_patchwork.workflow import load_run
from kci_patchwork.workflow import prepare
from kci_patchwork.workflow import reconcile
from kci_patchwork.workflow import submit

from patchwork.kernelci import can_test
from patchwork.kernelci import target_selection
from patchwork.models import KernelCIRun


class LocalSeries(Patchwork):
    """Use the existing series validator without HTTP back to Patchwork."""

    def __init__(self, series):
        if series.dependencies.exists():
            raise WorkflowError('Series dependencies are not supported')
        patches = list(series.patches.order_by('number', 'id')[:33])
        if any(p.project_id != series.project_id for p in patches):
            raise WorkflowError('Series contains patches from another project')
        self.source = None
        self.api_url = 'local:patchwork'
        self.series_data = {
            'id': series.pk,
            'name': series.name,
            'version': series.version,
            'date': series.date.isoformat(),
            'web_url': series.get_absolute_url(),
            'project': {'link_name': series.project.linkname},
            'total': series.total,
            'received_total': len(patches),
            'received_all': len(patches) == series.total,
            'patches': [{'id': p.pk} for p in patches],
        }
        self.patches = {
            p.pk: {
                'id': p.pk,
                'number': p.number,
                'name': p.name,
                'diff': p.diff,
                'msgid': p.msgid,
                'web_url': p.get_absolute_url(),
                'series': [{'id': series.pk}],
            }
            for p in patches
        }

    def get(self, resource, object_id):
        if resource == 'series' and object_id == self.series_data['id']:
            return self.series_data
        if resource == 'patches':
            return self.patches[object_id]
        raise WorkflowError('Unexpected local Patchwork resource')


def select_checkout(client, selection):
    """Find the newest eligible checkout only within this kernel tree/branch.

    Exhaust the bounded window before selecting: the API's default ordering
    does not guarantee newest-first results.
    """

    def eligible(node):
        snapshot = checkout_snapshot(node, node['id'])
        revision = snapshot['revision']
        if (
            revision['url'] != selection['git_url']
            or revision['branch'] != selection['branch']
        ):
            raise WorkflowError(
                'Checkout is outside the configured kernel tree'
            )
        return snapshot['id']

    if selection['checkout']:
        node = client.get_node(selection['checkout'], api_url=selection['api'])
        if node.get('id') != selection['checkout']:
            raise WorkflowError('KernelCI returned a different checkout')
        return eligible(node)
    since = datetime.now(datetime_timezone.utc) - timedelta(days=7)
    filters = [
        'name=checkout',
        'kind=checkout',
        'result=pass',
        f'created__gt={since.isoformat()}',
        'data.kernel_revision.url=' + selection['git_url'],
        'data.kernel_revision.branch=' + selection['branch'],
    ]
    candidates = []
    seen = set()
    for offset in range(0, 1000, 100):
        page = client.get_nodes(
            filters=filters, limit=100, offset=offset, api_url=selection['api']
        )
        if not isinstance(page, list) or len(page) > 100:
            raise WorkflowError('Invalid checkout listing')
        for node in page:
            node_id = node.get('id')
            if node_id in seen:
                raise WorkflowError('Checkout pagination repeated a node')
            seen.add(node_id)
            try:
                eligible(node)
                created = parse_datetime(node.get('created', ''))
                if created is None:
                    continue
                if timezone.is_naive(created):
                    created = created.replace(tzinfo=datetime_timezone.utc)
                if created >= since:
                    candidates.append((created, node_id))
            except (WorkflowError, KeyError, TypeError, ValueError):
                continue
        if len(page) < 100:
            break
    else:
        raise WorkflowError(
            'Checkout search truncated; configure an explicit checkout'
        )
    if not candidates:
        raise WorkflowError(
            'No eligible checkout; configure an explicit checkout'
        )
    return max(candidates)[1]


def run_directory(run):
    return Path(settings.KERNELCI_RUN_ROOT) / str(run.pk)


def _check_policy(run):
    if not can_test(run.user, run.series):
        raise WorkflowError('User or kernel project is no longer enabled')
    if target_selection(run.series.project, run.target) != run.selection:
        raise WorkflowError('KernelCI target changed since preparation')


def _poll(run, client):
    run.report = collect(client, run_directory(run), max_nodes=1000)
    now = timezone.now()
    finished = run.report['status'] == 'PATCH_APPLICATION_FAILED' or (
        run.report['terminal']
        and run.report.get('comparison', {}).get('complete')
    )
    run.state = (
        KernelCIRun.State.COMPLETE
        if finished or (run.poll_until and now >= run.poll_until)
        else KernelCIRun.State.RUNNING
    )
    run.next_poll = now + timedelta(seconds=60)


def process_run(run, client=None):
    """Advance one run under the management command's exclusive worker lock."""
    client = client or KernelCIClient()
    directory = run_directory(run)
    try:
        _check_policy(run)
        run.error = ''
        if run.state in (
            KernelCIRun.State.QUEUED,
            KernelCIRun.State.PREPARING,
        ):
            run.state = KernelCIRun.State.PREPARING
            run.save()
            if directory.exists():
                # A crash after preparation may leave complete durable files.
                manifest, state, _ = load_run(directory)
                if state['status'] != 'prepared':
                    raise WorkflowError('Unexpected preparation state')
            else:
                manifest = prepare(
                    client,
                    series_id=run.series_id,
                    checkout_id=select_checkout(client, run.selection),
                    job_filter=run.selection['jobs'],
                    platform_filter=run.selection['platforms'],
                    api_url=run.selection['api'],
                    pipeline_url=run.selection['pipeline'],
                    out=directory,
                    patchwork=LocalSeries(run.series),
                )
            run.manifest = manifest
            if (
                manifest['checkout']['revision']['url']
                != run.selection['git_url']
                or manifest['checkout']['revision']['branch']
                != run.selection['branch']
            ):
                raise WorkflowError(
                    'Prepared checkout is outside the configured tree'
                )
            run.state = KernelCIRun.State.PREPARED
        elif run.state in (
            KernelCIRun.State.SUBMIT_QUEUED,
            KernelCIRun.State.SUBMITTING,
        ):
            _, state, _ = load_run(directory)
            if state['status'] == 'prepared':
                if not settings.KERNELCI_TOKEN:
                    raise WorkflowError(
                        'KernelCI worker token is not configured'
                    )
                run.state = KernelCIRun.State.SUBMITTING
                run.save()
                state = submit(
                    client, directory, token=settings.KERNELCI_TOKEN
                )
            if state['status'] != 'submitted':
                run.state = KernelCIRun.State.UNKNOWN
                run.error = (
                    'Submission outcome is uncertain. Ask an operator to '
                    'reconcile it; do not create another run.'
                )
            else:
                run.state = KernelCIRun.State.RUNNING
                run.poll_until = timezone.now() + timedelta(hours=24)
                run.save()
                _poll(run, client)
        elif run.state == KernelCIRun.State.RUNNING:
            _poll(run, client)
    except Exception as exc:
        # API exceptions can contain response bodies or credentials. Never
        # persist their text or display it in the browser.
        if run.state == KernelCIRun.State.RUNNING:
            run.report = {
                'status': 'EVIDENCE_INCOMPLETE',
                'terminal': False,
                'approved': False,
                'required_coverage': 'not_assessed',
            }
            run.next_poll = timezone.now() + timedelta(seconds=60)
            if run.poll_until and timezone.now() >= run.poll_until:
                run.state = KernelCIRun.State.COMPLETE
        elif run.state in (
            KernelCIRun.State.SUBMITTING,
            KernelCIRun.State.SUBMIT_QUEUED,
        ):
            run.state = KernelCIRun.State.UNKNOWN
            try:
                _, saved_state, _ = load_run(directory)
                if saved_state['status'] == 'prepared':
                    # No durable attempt exists, so the user can retry after
                    # the operator fixes the token or configuration.
                    run.state = KernelCIRun.State.PREPARED
                elif saved_state['status'] == 'submitted':
                    run.state = KernelCIRun.State.RUNNING
                    run.poll_until = timezone.now() + timedelta(hours=24)
            except Exception:
                pass
        else:
            run.state = KernelCIRun.State.FAILED
        run.error = (
            'KernelCI could not finish this operation (%s). '
            'Ask an operator to inspect the configuration and saved run. '
            'An uncertain submission must be reconciled, never resubmitted.'
        ) % type(exc).__name__
    run.save()


def reconcile_run(run, node_id, client=None):
    _check_policy(run)
    if run.state != KernelCIRun.State.UNKNOWN:
        raise WorkflowError('Only uncertain submissions can be reconciled')
    client = client or KernelCIClient()
    manifest, state, _ = load_run(run_directory(run))
    if state['status'] == 'submitted':
        # The operator's previous reconciliation may have persisted its
        # library record before a failed database save. Verify it again.
        if state['patchset_node_id'] != node_id:
            raise WorkflowError('Node differs from the recorded submission')
        check_patchset(
            client.get_node(node_id, api_url=manifest['endpoints']['api']),
            manifest,
            node_id=node_id,
            treeid=state['treeid'],
            require_hash=True,
        )
    else:
        reconcile(client, run_directory(run), node_id=node_id)
    run.state = KernelCIRun.State.RUNNING
    run.error = ''
    run.poll_until = timezone.now() + timedelta(hours=24)
    run.next_poll = timezone.now()
    run.save()
