# SPDX-License-Identifier: GPL-2.0-or-later

import fcntl
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db.models import Q
from django.utils import timezone

from patchwork.models import KernelCIRun


class Command(BaseCommand):
    help = 'Process queued KernelCI runs and collect results once (schedule periodically)'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=20)
        parser.add_argument('--reconcile', metavar='RUN_UUID')
        parser.add_argument(
            '--node-id', help='Operator-verified patchset node ID'
        )

    def handle(self, *args, **options):
        if bool(options['reconcile']) != bool(options['node_id']):
            raise CommandError(
                '--reconcile and --node-id must be used together'
            )
        if not 1 <= options['limit'] <= 100:
            raise CommandError('--limit must be between 1 and 100')
        root = Path(settings.KERNELCI_RUN_ROOT)
        if not settings.KERNELCI_RUN_ROOT or not root.is_absolute():
            raise CommandError(
                'Set KERNELCI_RUN_ROOT to a private absolute path'
            )
        try:
            from patchwork.kernelci_worker import process_run
            from patchwork.kernelci_worker import reconcile_run
        except ImportError as exc:
            raise CommandError(
                'Install requirements-kernelci.txt for this worker'
            ) from exc
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (root / '.worker.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.stdout.write('Another KernelCI worker is active')
                return
            if options['reconcile']:
                try:
                    run = KernelCIRun.objects.select_related(
                        'series__project', 'user'
                    ).get(pk=options['reconcile'])
                    reconcile_run(run, options['node_id'])
                except Exception as exc:
                    raise CommandError(
                        'Reconciliation refused; check the saved run and node identity'
                    ) from exc
                self.stdout.write(str(run.pk) + ': ' + run.state)
                return
            pending = Q(
                state__in=[
                    KernelCIRun.State.QUEUED,
                    KernelCIRun.State.PREPARING,
                    KernelCIRun.State.SUBMIT_QUEUED,
                    KernelCIRun.State.SUBMITTING,
                ]
            ) | Q(
                state=KernelCIRun.State.RUNNING, next_poll__lte=timezone.now()
            )
            runs = (
                KernelCIRun.objects.filter(pending)
                .select_related('series__project', 'user')
                .order_by('updated')[: options['limit']]
            )
            for run in runs:
                process_run(run)
                self.stdout.write(str(run.pk) + ': ' + run.state)
