from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect


@login_required
def settings_index(request):
    # La página genérica de Ajustes era un placeholder: se redirige a Ajustes del espacio (o de la organización).
    workspace = getattr(request, "workspace", None)
    if workspace:
        return redirect("workspaces:settings", workspace_id=workspace.id)
    return redirect("organizations:settings")
