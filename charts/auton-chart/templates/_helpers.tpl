{{/* Common helpers. Resource names are fixed on purpose: docs, generated n8n
   workflows and the workers all use "auton:8080" by DNS. */}}

{{- define "auton.name" -}}
{{- default "auton" .Values.nameOverride -}}
{{- end -}}

{{- define "auton.namespace" -}}
{{- .Values.namespace.name -}}
{{- end -}}

{{- define "auton.appsNamespace" -}}
{{- .Values.appsNamespace.name -}}
{{- end -}}

{{- define "auton.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "auton.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: auton
{{- end -}}

{{- define "auton.selectorLabels" -}}
app.kubernetes.io/name: {{ include "auton.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "auton.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}

{{/* Bearer token: explicit value > existing Secret > generated (kept on upgrade). */}}
{{- define "auton.apiToken" -}}
{{- if .Values.agent.auth.token -}}
{{- .Values.agent.auth.token -}}
{{- else -}}
{{- $name := default "auton-api-token" .Values.agent.auth.secretName -}}
{{- $secret := lookup "v1" "Secret" (include "auton.namespace" .) $name -}}
{{- if and $secret (hasKey $secret.data "token") -}}
{{- index $secret.data "token" | b64dec -}}
{{- else -}}
{{- randAlphaNum 48 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "auton.tokenSecretName" -}}
{{- default "auton-api-token" .Values.agent.auth.secretName -}}
{{- end -}}

{{- define "auton.agentClaim" -}}
{{- default "agent-workspace" .Values.agent.workspace.existingClaim -}}
{{- end -}}

{{/* The shared workspace: audit log, incident store and the agent's files. */}}
{{- define "auton.workspaceClaim" -}}
{{- include "auton.agentClaim" . -}}
{{- end -}}

{{- define "auton.modelSecretName" -}}
{{- default "auton-model" .Values.provider.secretName -}}
{{- end -}}

{{/* The Secret that holds the queue password. */}}
{{- define "auton.redisSecretName" -}}
{{- if .Values.queue.redisExistingSecret -}}
{{- .Values.queue.redisExistingSecret -}}
{{- else -}}
{{- default "auton-redis" .Values.queue.redisSecretName -}}
{{- end -}}
{{- end -}}

{{/* URL every pod uses to reach the queue. */}}
{{- define "auton.redisUrl" -}}
{{- if .Values.queue.redisUrl -}}
{{- .Values.queue.redisUrl -}}
{{- else -}}
{{- printf "redis://redis.%s.svc.cluster.local:6379/0" (include "auton.namespace" .) -}}
{{- end -}}
{{- end -}}

{{/* The queue password: inline value > existing Secret > generated (kept).

     The generated value is stable across upgrades: `lookup` finds the Secret
     that the previous release created, so a `helm upgrade` never locks the
     running clients out. */}}
{{- define "auton.redisPassword" -}}
{{- if .Values.queue.redisPassword -}}
{{- .Values.queue.redisPassword -}}
{{- else -}}
{{- $name := default "auton-redis" .Values.queue.redisSecretName -}}
{{- $secret := lookup "v1" "Secret" (include "auton.namespace" .) $name -}}
{{- if and $secret (hasKey $secret.data "password") -}}
{{- index $secret.data "password" | b64dec -}}
{{- else -}}
{{- randAlphaNum 48 -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "auton.n8nUrl" -}}
{{- printf "http://n8n.%s.svc.cluster.local:5678" (include "auton.namespace" .) -}}
{{- end -}}
