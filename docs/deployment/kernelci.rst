KernelCI integration
====================

The optional KernelCI integration lets active, signed-in users prepare and test
a complete Linux kernel patch series. Administrators explicitly enable kernel
projects and define their kernel trees, branches and jobs. Other projects and
anonymous visitors cannot access the workflow, including its direct URLs.

The integration uses the pinned ``kci-patchwork`` library and its ``kci-dev``
Python API dependency. Patchwork reads its own series and diffs from the
database; enabling the REST API or fetching a public Patchwork URL is not
necessary. KernelCI calls run in a separate management command, never in web
requests. Normal Patchwork operation does not require these extra packages.

Installation and configuration
------------------------------

Install the optional dependency in the worker's environment and apply the
database migration using your normal production settings:

.. code-block:: shell

   python -m pip install -r requirements-kernelci.txt
   python manage.py migrate

Add settings like these to the production configuration:

.. code-block:: python

   import os

   KERNELCI_API_URL = 'https://staging.kernelci.org:9000'
   KERNELCI_PIPELINE_URL = 'https://staging.kernelci.org:9100'
   KERNELCI_TOKEN = os.environ.get('KCI_PIPELINE_TOKEN', '')
   KERNELCI_RUN_ROOT = '/var/lib/patchwork/kernelci'
   KERNELCI_DAILY_LIMIT = 10
   KERNELCI_PROJECTS = {
       # Use the exact Project.linkname of your Linux kernel mailing list.
       'linux': {
           'mainline-tinyconfig': {
               'label': 'Mainline kernel / x86 tinyconfig',
               'git_url': 'https://github.com/kernelci/linux.git',
               'branch': 'staging-mainline',
               'jobs': ['kbuild-gcc-14-x86-tinyconfig'],
               # Optional: pin an existing successful checkout node ID.
               # 'checkout': 'REPLACE_WITH_24_HEX_NODE_ID',
               # Optional: restrict platforms. Otherwise inherit the base.
               # 'platforms': ['REPLACE_WITH_SUPPORTED_PLATFORM'],
           },
       },
   }

These are example target values, not a guarantee that those jobs or checkouts
exist on your deployment. Use the Git URL and branch recorded by your KernelCI
instance and choose jobs which cover the changed code. For CIP or subsystem
kernels, add a separate target with that kernel's exact repository and branch.
Never select an unrelated global "latest checkout". Project names alone do not
prove kernel membership: ``KERNELCI_PROJECTS`` is the administrator's explicit
allowlist of kernel projects.

The API and pipeline URLs must identify the same KernelCI instance and use
HTTPS. Provide a pipeline token with patchset submission permission to the
worker through its environment. Browser forms cannot select credentials,
endpoints, arbitrary Git repositories, checkout IDs or job filters. Tokens are
not included in the saved selection, manifests, reports or pages.

If ``checkout`` is omitted, preparation examines up to 1,000 checkouts from the
last seven days in the configured tree and branch, and selects the newest
eligible one by its creation timestamp. The checkout must have passed, have a
source tarball and contain no existing patchset. A truncated or empty search
fails instead of guessing. Set an explicit checkout for older kernels or busy
trees. The prepared page always shows the exact commit selected.

Keep ``KERNELCI_RUN_ROOT`` on a private local filesystem outside static/media
directories. Give only the worker account access. Every worker for this
Patchwork database must use the same directory on the same host; the command
uses ``flock`` to exclude concurrent workers. Back up this directory together
with the database. Do not delete attempt records to retry a submission.

Run the worker
--------------

.. code-block:: shell

   python manage.py kernelci_worker --limit 20

This processes up to 20 eligible runs once, then exits. Schedule it once per
minute with your service scheduler, using the same database and settings as the
web application and a worker-only token environment. An overlapping invocation
exits without doing work. A new installation remains disabled until projects,
endpoints, credentials and the worker have been configured.

The web application queues work in the database. The worker prepares manifests
and exact ordered patch files under ``KERNELCI_RUN_ROOT/<run UUID>/``. A run is
not submitted until its owner explicitly presses **Submit to KernelCI**.
Submission intent is recorded before contacting the pipeline. The library also
persists its attempt before the HTTP POST, and uncertain attempts are never
automatically retried.

Results are collected no more often than once a minute, with a limit of 1,000
nodes per patched tree and baseline collection. Collection continues until the
comparison completes, patch application fails, or 24 hours elapse. Owners can
request another collection period using **Refresh results**, which does not
submit jobs. Worker throughput and remote API latency can make intervals longer.

User workflow
-------------

#. Sign in and open a patch or cover letter in an enabled kernel project.
#. Choose **Test with KernelCI**, select a configured target and press
   **Prepare series**.
#. Reload the run page after the worker prepares it. Review the kernel tree,
   branch, exact commit, complete ordered series, jobs and effective platforms.
#. Press **Submit to KernelCI** to queue one submission.
#. Reload to see patch application, job results and baseline comparisons.
   Download the JSON report for the full collected evidence.

All new pages and reports are private to the initiating user and require an
active session. Actions require POST and CSRF protection. The underlying
KernelCI instance controls the visibility of submitted patches and results;
Patchwork's private run page does not make a public KernelCI instance private.

Each user can prepare ten new runs per rolling 24 hours by default. There is
one retained run per user, series and target key, including completed or failed
runs. Repeated clicks reopen that run. Use a new, explicitly configured target
key when intentionally offering a new baseline or another run. Configuration
changes to an existing target block pending work, so retain old target entries
while their runs are active. Disabling a project or account stops its worker
operations. Runs protect their referenced series and user from deletion;
administrators must deliberately handle their records before deleting either.

Recovery and result interpretation
----------------------------------

For an uncertain submission, an operator must find and inspect the actual
patchset in KernelCI. Then reconcile its node with the saved attempt:

.. code-block:: shell

   python manage.py kernelci_worker \
       --reconcile RUN_UUID --node-id PATCHSET_NODE_ID

Reconciliation only reads KernelCI. It checks the parent checkout, revision,
job and platform filters, attempt time and pipeline patchset hash; it does not
submit again. The hash becomes available after patch application and is not a
full-byte identity proof, so the operator must verify the actual patch artifacts.
An unrelated node is refused. Interrupted preparation with complete files and
interrupted submission with a recorded response recover on the next worker run.
Partial or corrupted files require operator inspection, not blind recreation.

Missing or ambiguous evidence stays ``EVIDENCE_INCOMPLETE``. A matched baseline
pass becoming a failure is ``REGRESSIONS_DETECTED``; it is evidence to inspect,
not proof of causality. A complete comparison without failures is
``NO_FAILURES_IN_OBSERVED_RESULTS``. Existing baseline failures can produce
``NO_REGRESSIONS_IN_OBSERVED_RESULTS``. These statuses cover observed jobs only;
they neither assess required coverage nor approve a patch series. The integration
does not post Patchwork checks, comments or mail, and does not change patch state.

The initial integration supports complete series containing 1 to 32 text
patches, up to 10 MiB per diff and 32 MiB total, following the library contract.
Binary patches, standalone patches without a series and series with declared
dependencies are rejected. Required job coverage and dependency-series assembly
are outside this workflow.

Development tests
-----------------

The integration tests block external HTTP and use simulated KernelCI responses:

.. code-block:: shell

   tox -e kernelci

The standard test environments also run the web access tests. Worker tests skip
when the optional dependency is absent. The dedicated environment installs the
pinned dependency and exercises preparation, submission, uncertain-outcome
recovery and the actual library's baseline comparison code.
