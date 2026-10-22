{{/*
Expand the name of the chart.
*/}}
{{- define "scope-ui.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "scope-ui.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "scope-ui.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "scope-ui.labels" -}}
helm.sh/chart: {{ include "scope-ui.chart" . }}
{{ include "scope-ui.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "scope-ui.selectorLabels" -}}
app.kubernetes.io/name: {{ include "scope-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "scope-ui.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "scope-ui.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Return the secret containing the scope-ui config secrets
*/}}
{{- define "config.secretName" -}}
{{- $secretName := printf "%s-config-secret" (include "scope-ui.name" .) | trunc 63 | trimSuffix "-" -}}
{{- $config := .Values.config | default dict -}}
{{- if and (not .Values.externalSecrets) (get $config "secretName") -}}
    {{- $secretName = get $config "secretName" -}}
{{- end -}}
{{- printf "%s" (tpl $secretName $) -}}
{{- end -}}

{{/*
Return the secret containing the tls certificate secrets
*/}}
{{- define "TLSCertificate.secretName" -}}
{{- $secretName := printf "%s-tls-secret" (include "scope-ui.name" .) | trunc 63 | trimSuffix "-" -}}
{{- $tlsCertificate := .Values.TLSCertificate | default dict -}}
{{- if and (not .Values.externalSecrets) (get $tlsCertificate "secretName") -}}
    {{- $secretName = get $tlsCertificate "secretName" -}}
{{- end -}}
{{- printf "%s" (tpl $secretName $) -}}
{{- end -}}

{{/*
Resolve the internal base URL of the API service consumed by the UI.
*/}}
{{- define "scope-ui.apiBaseUrl" -}}
{{- $global := get .Values "global" | default dict -}}
{{- $baseName := get $global "baseName" | default "scope" -}}
{{- $defaultBaseUrl := printf "http://%s-api:8000" $baseName -}}
{{- $baseUrl := .Values.scopeApiUrl | default $defaultBaseUrl -}}
{{- $baseUrl -}}
{{- end -}}

{{/*
Resolve the API base URL used by nginx proxy_pass.
Must always be an absolute URL to avoid nginx startup failures.
*/}}
{{- define "scope-ui.apiProxyBaseUrl" -}}
{{- $global := get .Values "global" | default dict -}}
{{- $baseName := get $global "baseName" | default "scope" -}}
{{- printf "http://%s-api:8000" $baseName -}}
{{- end -}}

{{/*
Resolve the REST API URL.
*/}}
{{- define "scope-ui.restApiUrl" -}}
{{- $defaultUrl := printf "%s/api/v1" (include "scope-ui.apiBaseUrl" .) -}}
{{- $url := .Values.ui.restApiUrl | default $defaultUrl -}}
{{- $url -}}
{{- end -}}