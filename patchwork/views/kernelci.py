# SPDX-License-Identifier: GPL-2.0-or-later

from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404
from django.http import HttpResponse
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import redirect
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_http_methods
from django.views.decorators.http import require_POST

from patchwork.kernelci import can_test
from patchwork.kernelci import target_selection
from patchwork.kernelci import targets_for
from patchwork.models import KernelCIRun
from patchwork.models import Series


class TargetForm(forms.Form):
    target = forms.ChoiceField(label='Kernel test target')

    def __init__(self, targets, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['target'].choices = [
            (name, target.get('label', name))
            for name, target in targets.items()
        ]


def _run(request, run_id):
    run = get_object_or_404(
        KernelCIRun.objects.select_related('series__project'),
        pk=run_id,
        user=request.user,
    )
    if not can_test(request.user, run.series):
        raise Http404
    return run


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def series(request, series_id):
    selected = get_object_or_404(
        Series.objects.select_related('project'), pk=series_id
    )
    if not can_test(request.user, selected):
        raise Http404
    form = TargetForm(targets_for(selected.project), request.POST or None)
    if request.method == 'POST' and form.is_valid():
        if not 1 <= selected.total <= 32 or (
            selected.received_total != selected.total
        ):
            form.add_error(
                None, 'A complete series of 1 to 32 patches is required.'
            )
        elif selected.dependencies.exists():
            form.add_error(
                None, 'Series with dependencies are not supported yet.'
            )
        else:
            target = form.cleaned_data['target']
            selection = target_selection(selected.project, target)
            with transaction.atomic():
                # Serialize per-user quota checks as well as double clicks.
                get_user_model().objects.select_for_update().get(
                    pk=request.user.pk
                )
                runs = KernelCIRun.objects.filter(user=request.user)
                existing = runs.filter(series=selected, target=target).first()
                if existing:
                    return redirect(existing)
                limit = settings.KERNELCI_DAILY_LIMIT
                if (
                    runs.filter(
                        created__gte=timezone.now() - timedelta(days=1)
                    ).count()
                    >= limit
                ):
                    form.add_error(
                        None, 'Your daily preparation limit was reached.'
                    )
                else:
                    run = KernelCIRun.objects.create(
                        user=request.user,
                        series=selected,
                        target=target,
                        selection=selection,
                    )
                    return redirect(run)
    return render(
        request,
        'patchwork/kernelci-series.html',
        {
            'project': selected.project,
            'series': selected,
            'form': form,
            'runs': KernelCIRun.objects.filter(
                user=request.user, series=selected
            ),
        },
    )


@login_required
@never_cache
@require_GET
def detail(request, run_id):
    run = _run(request, run_id)
    return render(
        request,
        'patchwork/kernelci-run.html',
        {'project': run.series.project, 'run': run},
    )


@login_required
@never_cache
@require_POST
def submit(request, run_id):
    run = _run(request, run_id)
    if run.target not in targets_for(run.series.project) or (
        target_selection(run.series.project, run.target) != run.selection
    ):
        return HttpResponse(
            'The test target changed. Contact an administrator.', status=409
        )
    # Atomic transition: concurrent POSTs can enqueue only one submission.
    KernelCIRun.objects.filter(
        pk=run.pk, state=KernelCIRun.State.PREPARED
    ).update(
        state=KernelCIRun.State.SUBMIT_QUEUED, updated=timezone.now(), error=''
    )
    return redirect(run)


@login_required
@never_cache
@require_POST
def refresh(request, run_id):
    run = _run(request, run_id)
    KernelCIRun.objects.filter(
        pk=run.pk, state=KernelCIRun.State.COMPLETE
    ).update(
        state=KernelCIRun.State.RUNNING,
        next_poll=timezone.now(),
        poll_until=timezone.now() + timedelta(hours=24),
        updated=timezone.now(),
    )
    return redirect(run)


@login_required
@never_cache
@require_GET
def report(request, run_id):
    run = _run(request, run_id)
    response = JsonResponse(
        run.report or {'status': run.state, 'selection': run.manifest}
    )
    response['Content-Disposition'] = (
        'attachment; filename="kernelci-%s.json"' % run.pk
    )
    return response
