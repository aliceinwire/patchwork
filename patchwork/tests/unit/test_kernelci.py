# SPDX-License-Identifier: GPL-2.0-or-later

from copy import deepcopy
from datetime import datetime
from datetime import timedelta
from datetime import timezone as datetime_timezone
import fcntl
from importlib.util import find_spec
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from patchwork.kernelci import target_selection
from patchwork.models import KernelCIRun
from patchwork.tests.utils import create_cover
from patchwork.tests.utils import create_patch
from patchwork.tests.utils import create_project
from patchwork.tests.utils import create_series
from patchwork.tests.utils import create_user
from patchwork.tests.utils import SAMPLE_DIFF


HAS_WORKER = find_spec('kci_patchwork') is not None
if HAS_WORKER:
    from kcidev import KernelCIClient
    from kci_patchwork.patchwork import pipeline_hash
    from kci_patchwork.storage import WorkflowError
    from kci_patchwork.storage import write_json
    from kci_patchwork.workflow import load_run
    from patchwork.kernelci_worker import LocalSeries
    from patchwork.kernelci_worker import process_run
    from patchwork.kernelci_worker import run_directory
    from patchwork.kernelci_worker import select_checkout


BASE_ID, PATCHSET_ID = '1' * 24, '2' * 24
BASE_TREE, PATCHSET_TREE = 'a' * 64, 'b' * 64
JOB = 'kbuild-gcc-14-x86-tinyconfig'
API = 'https://api.example.test'
PIPELINE = 'https://pipeline.example.test'
GIT = 'https://git.example.test/linux.git'


class KernelCIBase(TestCase):
    def setUp(self):
        self.user = create_user()
        self.project = create_project(linkname='linux')
        self.series = create_series(project=self.project, total=2)
        self.first = create_patch(series=self.series, number=1)
        self.second = create_patch(series=self.series, number=2)
        self.projects = {
            'linux': {
                'mainline': {
                    'label': 'Mainline kernel',
                    'git_url': GIT,
                    'branch': 'master',
                    'checkout': BASE_ID,
                    'jobs': [JOB],
                },
            },
        }
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        override = self.settings(
            KERNELCI_PROJECTS=self.projects,
            KERNELCI_API_URL=API,
            KERNELCI_PIPELINE_URL=PIPELINE,
            KERNELCI_RUN_ROOT=self.root,
            KERNELCI_TOKEN='private-pipeline-token',
        )
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(self.user)
        self.series_url = reverse('kernelci-series', args=[self.series.pk])

    def make_run(self, state=KernelCIRun.State.QUEUED):
        return KernelCIRun.objects.create(
            user=self.user,
            series=self.series,
            target='mainline',
            state=state,
            selection=target_selection(self.project, 'mainline'),
        )


class KernelCIViewTest(KernelCIBase):
    def test_anonymous_cannot_read_or_write_any_endpoint(self):
        run = self.make_run()
        self.client.logout()
        urls = [self.series_url] + [
            reverse(name, args=[run.pk])
            for name in (
                'kernelci-run',
                'kernelci-submit',
                'kernelci-refresh',
                'kernelci-report',
            )
        ]
        for url in urls:
            for method in (self.client.get, self.client.post):
                with self.subTest(url=url, method=method):
                    self.assertEqual(method(url).status_code, 302)
        run.refresh_from_db()
        self.assertEqual(run.state, 'queued')

    def test_only_enabled_kernel_projects(self):
        with self.settings(KERNELCI_PROJECTS={}):
            self.assertEqual(self.client.get(self.series_url).status_code, 404)
            self.assertEqual(
                self.client.post(
                    self.series_url, {'target': 'mainline'}
                ).status_code,
                404,
            )
        self.assertFalse(KernelCIRun.objects.exists())

    def test_run_is_private_to_its_owner(self):
        run = self.make_run('prepared')
        self.client.force_login(create_user())
        for name in (
            'kernelci-run',
            'kernelci-report',
            'kernelci-submit',
            'kernelci-refresh',
        ):
            method = (
                self.client.post
                if name in ('kernelci-submit', 'kernelci-refresh')
                else self.client.get
            )
            self.assertEqual(
                method(reverse(name, args=[run.pk])).status_code, 404
            )

    def test_get_only_displays_form(self):
        self.assertContains(self.client.get(self.series_url), 'Prepare series')
        self.assertFalse(KernelCIRun.objects.exists())

    def test_target_cannot_be_supplied_by_user(self):
        response = self.client.post(
            self.series_url,
            {'target': 'https://evil.test', 'checkout': 'f' * 24},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(KernelCIRun.objects.exists())

    def test_complete_series_is_queued_once(self):
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    self.series_url, {'target': 'mainline'}
                ).status_code,
                302,
            )
        run = KernelCIRun.objects.get()
        self.assertEqual(run.state, 'queued')
        self.assertEqual(run.selection['checkout'], BASE_ID)
        self.assertNotIn('private-pipeline-token', json.dumps(run.selection))

    def test_incomplete_series_is_rejected(self):
        self.second.delete()
        response = self.client.post(self.series_url, {'target': 'mainline'})
        self.assertContains(response, 'A complete series')
        self.assertFalse(KernelCIRun.objects.exists())

    def test_dependencies_are_rejected(self):
        self.series.dependencies.add(create_series(project=self.project))
        response = self.client.post(self.series_url, {'target': 'mainline'})
        self.assertContains(response, 'dependencies are not supported')
        self.assertFalse(KernelCIRun.objects.exists())

    def test_daily_limit(self):
        with self.settings(KERNELCI_DAILY_LIMIT=0):
            response = self.client.post(
                self.series_url, {'target': 'mainline'}
            )
        self.assertContains(response, 'daily preparation limit')
        self.assertFalse(KernelCIRun.objects.exists())

    def test_submit_requires_post_and_prepared_state(self):
        run = self.make_run()
        url = reverse('kernelci-submit', args=[run.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        run.refresh_from_db()
        self.assertEqual(run.state, 'queued')
        run.state = 'prepared'
        run.save()
        for _ in range(2):
            self.client.post(url)
        run.refresh_from_db()
        self.assertEqual(run.state, 'submit_queued')

    def test_csrf_protects_all_actions(self):
        run = self.make_run('prepared')
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        for url in [self.series_url] + [
            reverse(name, args=[run.pk])
            for name in ('kernelci-submit', 'kernelci-refresh')
        ]:
            self.assertEqual(
                client.post(url, {'target': 'mainline'}).status_code, 403
            )
        run.refresh_from_db()
        self.assertEqual(run.state, 'prepared')

    def test_target_change_prevents_submission(self):
        run = self.make_run('prepared')
        self.projects['linux']['mainline']['branch'] = 'other'
        self.assertEqual(
            self.client.post(
                reverse('kernelci-submit', args=[run.pk])
            ).status_code,
            409,
        )
        run.refresh_from_db()
        self.assertEqual(run.state, 'prepared')

    def test_refresh_cannot_resubmit(self):
        run = self.make_run('complete')
        self.client.post(reverse('kernelci-refresh', args=[run.pk]))
        run.refresh_from_db()
        self.assertEqual(run.state, 'running')

    def test_private_pages_are_not_cached_and_escape_content(self):
        run = self.make_run()
        run.report = {'status': '<script>alert(1)</script>'}
        run.save()
        response = self.client.get(run.get_absolute_url())
        self.assertIn('private', response['Cache-Control'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, '&lt;script&gt;')
        report = self.client.get(reverse('kernelci-report', args=[run.pk]))
        self.assertEqual(report.json()['status'], run.report['status'])
        self.assertIn('attachment', report['Content-Disposition'])

    def test_buttons_only_on_authenticated_kernel_pages(self):
        cover = create_cover(series=self.series)
        for submission in (self.first, cover):
            url = submission.get_absolute_url()
            self.assertContains(self.client.get(url), 'Test with KernelCI')
            with self.settings(KERNELCI_PROJECTS={}):
                self.assertNotContains(
                    self.client.get(url), 'Test with KernelCI'
                )
        self.client.logout()
        self.assertNotContains(
            self.client.get(self.first.get_absolute_url()),
            'Test with KernelCI',
        )


@unittest.skipUnless(
    HAS_WORKER, 'Optional requirements-kernelci.txt not installed'
)
class KernelCIWorkerTest(KernelCIBase):
    def setUp(self):
        super().setUp()
        blocked = patch(
            'requests.sessions.Session.request',
            side_effect=AssertionError('Unexpected HTTP'),
        )
        blocked.start()
        self.addCleanup(blocked.stop)
        self.api = Mock(spec=KernelCIClient)
        self.base = {
            'id': BASE_ID,
            'treeid': BASE_TREE,
            'name': 'checkout',
            'kind': 'checkout',
            'state': 'done',
            'result': 'pass',
            'parent': None,
            'path': ['checkout'],
            'created': datetime.now(datetime_timezone.utc).isoformat(),
            'artifacts': {
                'tarball': 'https://files.example.test/source.tar.gz'
            },
            'data': {
                'kernel_revision': {
                    'url': GIT,
                    'branch': 'master',
                    'commit': 'c' * 40,
                }
            },
        }
        self.root_node = deepcopy(self.base) | {
            'id': PATCHSET_ID,
            'treeid': PATCHSET_TREE,
            'name': 'patchset',
            'parent': BASE_ID,
            'path': ['checkout', 'patchset'],
            'jobfilter': [JOB],
            'platform_filter': [],
        }
        self.root_node['data']['kernel_revision']['patchset'] = pipeline_hash(
            [SAMPLE_DIFF.encode()] * 2
        )
        self.api.get_node.side_effect = lambda ident, **kwargs: deepcopy(
            self.base if ident == BASE_ID else self.root_node
        )
        self.api.trigger_patchset.return_value = {'node': self.root_node}
        self.build = {
            'id': '3' * 24,
            'treeid': PATCHSET_TREE,
            'parent': PATCHSET_ID,
            'name': JOB,
            'kind': 'kbuild',
            'path': ['checkout', 'patchset', JOB],
            'state': 'done',
            'result': 'pass',
            'data': {
                'arch': 'x86_64',
                'compiler': 'gcc-14',
                'config_full': 'tinyconfig',
            },
        }
        self.before = deepcopy(self.build) | {
            'id': '4' * 24,
            'treeid': BASE_TREE,
            'parent': BASE_ID,
            'path': ['checkout', JOB],
        }

        def nodes(**kwargs):
            rows = (
                [self.root_node, self.build]
                if kwargs['filters'] == ['treeid=' + PATCHSET_TREE]
                else [self.before]
            )
            return deepcopy(
                rows[kwargs['offset'] : kwargs['offset'] + kwargs['limit']]
            )

        self.api.get_nodes.side_effect = nodes

    def prepared(self):
        run = self.make_run()
        process_run(run, self.api)
        self.assertEqual(run.state, 'prepared', run.error)
        return run

    def submitted(self):
        run = self.prepared()
        run.state = 'submit_queued'
        run.save()
        process_run(run, self.api)
        return run

    def test_local_series_uses_database_order(self):
        metadata, patches = LocalSeries(self.series).fetch_series(
            self.series.pk
        )
        self.assertEqual(metadata['project'], 'linux')
        self.assertEqual(
            [p['id'] for p in patches], [self.first.pk, self.second.pk]
        )
        self.assertEqual([p['position'] for p in patches], [1, 2])

    def test_invalid_order_and_binary_diffs_are_rejected(self):
        self.second.number = None
        self.second.save()
        with self.assertRaises(WorkflowError):
            LocalSeries(self.series).fetch_series(self.series.pk)
        self.second.number = 2
        self.second.diff = 'GIT binary patch\n'
        self.second.save()
        with self.assertRaises(WorkflowError):
            LocalSeries(self.series).fetch_series(self.series.pk)

    def test_prepare_never_submits_and_freezes_exact_bytes(self):
        run = self.prepared()
        self.api.trigger_patchset.assert_not_called()
        manifest, state, patches = load_run(run_directory(run))
        self.assertEqual(patches, [SAMPLE_DIFF, SAMPLE_DIFF])
        self.assertEqual(state['status'], 'prepared')
        self.assertEqual(manifest['checkout']['id'], BASE_ID)
        self.assertNotIn('private-pipeline-token', json.dumps(manifest))

    def test_round_trip_compares_the_exact_baseline(self):
        run = self.submitted()
        self.assertEqual(run.state, 'complete', run.error)
        self.assertEqual(
            run.report['status'], 'NO_FAILURES_IN_OBSERVED_RESULTS'
        )
        self.assertTrue(run.report['comparison']['complete'])
        self.assertFalse(run.report['approved'])
        self.api.trigger_patchset.assert_called_once_with(
            nodeid=BASE_ID,
            patches=[SAMPLE_DIFF, SAMPLE_DIFF],
            job_filter=[JOB],
            platform_filter=None,
            pipeline_url=PIPELINE,
            token='private-pipeline-token',
        )

    def test_regression_is_reported(self):
        self.build['result'] = 'fail'
        run = self.submitted()
        self.assertEqual(run.report['status'], 'REGRESSIONS_DETECTED')
        self.assertEqual(run.report['comparison']['counts']['regression'], 1)

    def test_submission_through_real_kci_dev_api(self):
        self.api.trigger_patchset.side_effect = (
            KernelCIClient().trigger_patchset
        )
        with patch('kcidev.libs.maestro_common.kcidev_session.post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {'node': self.root_node}
            run = self.submitted()
        self.assertEqual(run.state, 'complete', run.error)
        post.assert_called_once()
        self.assertEqual(post.call_args.args, (PIPELINE + '/api/patchset',))
        payload = json.loads(post.call_args.kwargs['data'])
        self.assertEqual(payload['patch'], [SAMPLE_DIFF, SAMPLE_DIFF])
        self.assertEqual(payload['nodeid'], BASE_ID)
        self.assertEqual(payload['jobfilter'], [JOB])
        self.assertNotIn('private-pipeline-token', json.dumps(run.report))
        self.assertContains(
            self.client.get(run.get_absolute_url()), 'unchanged_pass'
        )

    def test_missing_baseline_is_incomplete_not_pass(self):
        self.api.get_nodes.side_effect = lambda **kwargs: (
            [self.root_node, self.build]
            if kwargs['filters'] == ['treeid=' + PATCHSET_TREE]
            else []
        )
        run = self.submitted()
        self.assertEqual(run.report['status'], 'EVIDENCE_INCOMPLETE')
        self.assertEqual(run.state, 'running')

    def test_submission_timeout_never_retries(self):
        self.api.trigger_patchset.side_effect = TimeoutError(
            'private-pipeline-token'
        )
        run = self.submitted()
        self.assertEqual(run.state, 'unknown')
        self.assertNotIn('private-pipeline-token', run.error)
        process_run(run, self.api)
        self.api.trigger_patchset.assert_called_once()
        _, state, _ = load_run(run_directory(run))
        self.assertEqual(state['status'], 'submission_unknown')

    def test_interrupted_attempt_never_retries(self):
        run = self.prepared()
        _, state, _ = load_run(run_directory(run))
        state.update(
            status='submitting',
            attempted_at=datetime.now(datetime_timezone.utc).isoformat(),
        )
        write_json(run_directory(run) / 'state.json', state)
        run.state = 'submitting'
        run.save()
        process_run(run, self.api)
        self.assertEqual(run.state, 'unknown')
        self.api.trigger_patchset.assert_not_called()

    def test_submitted_files_recover_after_database_update_interruption(self):
        run = self.submitted()
        self.api.trigger_patchset.reset_mock()
        run.state = 'submitting'
        run.save()
        process_run(run, self.api)
        self.assertEqual(run.state, 'complete', run.error)
        self.api.trigger_patchset.assert_not_called()

    def test_prepared_files_recover_after_interruption(self):
        run = self.prepared()
        run.state = 'preparing'
        run.manifest = {}
        run.save()
        self.api.reset_mock()
        process_run(run, self.api)
        self.assertEqual(run.state, 'prepared', run.error)
        self.api.get_node.assert_not_called()

    def test_missing_token_leaves_unsubmitted_run_prepared(self):
        run = self.prepared()
        run.state = 'submit_queued'
        with self.settings(KERNELCI_TOKEN=''):
            process_run(run, self.api)
        self.assertEqual(run.state, 'prepared')
        self.api.trigger_patchset.assert_not_called()

    def test_changed_patch_file_cannot_be_submitted(self):
        run = self.prepared()
        file = run_directory(run) / run.manifest['patches'][0]['file']
        file.write_text(SAMPLE_DIFF + '+changed\n')
        run.state = 'submit_queued'
        process_run(run, self.api)
        self.assertEqual(run.state, 'unknown')
        self.api.trigger_patchset.assert_not_called()

    def test_removed_project_and_disabled_user_prevent_worker_calls(self):
        run = self.make_run()
        with self.settings(KERNELCI_PROJECTS={}):
            process_run(run, self.api)
        self.assertEqual(run.state, 'failed')
        run.state = 'queued'
        run.user.is_active = False
        run.user.save()
        process_run(run, self.api)
        self.assertEqual(run.state, 'failed')
        self.api.get_node.assert_not_called()
        self.api.trigger_patchset.assert_not_called()

    def test_latest_checkout_is_limited_to_configured_tree(self):
        selection = target_selection(self.project, 'mainline') | {
            'checkout': ''
        }
        older = deepcopy(self.base) | {
            'id': '5' * 24,
            'created': (
                datetime.now(datetime_timezone.utc) - timedelta(days=1)
            ).isoformat(),
        }
        wrong = deepcopy(self.base) | {'id': '6' * 24}
        wrong['data']['kernel_revision']['url'] = (
            'https://git.example.test/unrelated.git'
        )
        self.api.get_nodes.side_effect = None
        self.api.get_nodes.return_value = [self.base, wrong, older]
        self.assertEqual(select_checkout(self.api, selection), BASE_ID)
        filters = self.api.get_nodes.call_args.kwargs['filters']
        self.assertIn('data.kernel_revision.url=' + GIT, filters)
        self.assertIn('data.kernel_revision.branch=master', filters)

    def test_naive_kernelci_timestamp_is_utc_with_patchwork_naive_dates(self):
        selection = target_selection(self.project, 'mainline') | {
            'checkout': ''
        }
        self.base['created'] = (
            datetime.now(datetime_timezone.utc)
            .replace(tzinfo=None)
            .isoformat()
        )
        self.api.get_nodes.side_effect = None
        self.api.get_nodes.return_value = [self.base]
        self.assertEqual(select_checkout(self.api, selection), BASE_ID)

    def test_explicit_checkout_must_match_tree_and_branch(self):
        self.base['data']['kernel_revision']['branch'] = 'other'
        run = self.make_run()
        process_run(run, self.api)
        self.assertEqual(run.state, 'failed')
        self.api.trigger_patchset.assert_not_called()

    def test_latest_checkout_search_does_not_accept_truncated_evidence(self):
        selection = target_selection(self.project, 'mainline') | {
            'checkout': ''
        }
        self.api.get_nodes.side_effect = lambda **kwargs: [
            deepcopy(self.base) | {'id': f'{i + 1:024x}'}
            for i in range(
                kwargs['offset'], kwargs['offset'] + kwargs['limit']
            )
        ]
        with self.assertRaisesRegex(WorkflowError, 'truncated'):
            select_checkout(self.api, selection)

    def test_polling_deadline_preserves_incomplete_result(self):
        run = self.submitted()
        self.api.get_node.side_effect = TimeoutError('unavailable')
        run.state = 'running'
        run.poll_until = timezone.now() - timedelta(seconds=1)
        process_run(run, self.api)
        self.assertEqual(run.state, 'complete')
        self.assertEqual(run.report['status'], 'EVIDENCE_INCOMPLETE')
        self.assertFalse(run.report['approved'])

    def test_reconcile_recovers_only_operator_identified_node(self):
        self.api.trigger_patchset.side_effect = TimeoutError()
        run = self.submitted()
        self.root_node['created'] = (
            datetime.now(datetime_timezone.utc) + timedelta(seconds=1)
        ).isoformat()
        with patch(
            'patchwork.kernelci_worker.KernelCIClient', return_value=self.api
        ):
            call_command(
                'kernelci_worker',
                reconcile=str(run.pk),
                node_id=PATCHSET_ID,
                stdout=StringIO(),
            )
        run.refresh_from_db()
        self.assertEqual(run.state, 'running')
        self.api.trigger_patchset.assert_called_once()

    def test_worker_lock_excludes_second_worker(self):
        self.make_run()
        with (Path(self.root) / '.worker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            output = StringIO()
            with patch('patchwork.kernelci_worker.process_run') as advance:
                call_command('kernelci_worker', stdout=output)
                advance.assert_not_called()
            self.assertIn('Another KernelCI worker', output.getvalue())

    def test_reconciliation_recovers_after_database_save_interruption(self):
        run = self.submitted()
        run.state = 'unknown'
        run.save()
        self.api.trigger_patchset.reset_mock()
        with patch(
            'patchwork.kernelci_worker.KernelCIClient', return_value=self.api
        ):
            call_command(
                'kernelci_worker',
                reconcile=str(run.pk),
                node_id=PATCHSET_ID,
                stdout=StringIO(),
            )
        run.refresh_from_db()
        self.assertEqual(run.state, 'running')
        self.api.trigger_patchset.assert_not_called()

    def test_command_processes_queue_without_submitting(self):
        run = self.make_run()
        with patch(
            'patchwork.kernelci_worker.KernelCIClient', return_value=self.api
        ):
            call_command('kernelci_worker', stdout=StringIO())
        run.refresh_from_db()
        self.assertEqual(run.state, 'prepared', run.error)
        self.api.trigger_patchset.assert_not_called()
