{{/*
Expand the name of the chart.
*/}}
{{- define "scope-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "scope-api.fullname" -}}
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
{{- define "scope-api.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "scope-api.labels" -}}
helm.sh/chart: {{ include "scope-api.chart" . }}
{{ include "scope-api.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "scope-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "scope-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "scope-api.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "scope-api.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Return the secret containing the scope-api server secrets
*/}}
{{- define "scope-api.server.secretName" -}}
{{- $secretName := printf "%s-server-secret" (include "scope-api.name" .) | trunc 63 | trimSuffix "-" -}}
{{- if and (not .Values.externalSecrets) .Values.api.service.secretName -}}
    {{- $secretName = .Values.api.service.secretName -}}
{{- end -}}
{{- printf "%s" (tpl $secretName $) -}}
{{- end -}}

{{/*
Return the secret containing the clickhouse secrets
*/}}
{{- define "scope-api.clickhouse.secretName" -}}
{{- $secretName := printf "%s-clickhouse-secret" (include "scope-api.name" .) | trunc 63 | trimSuffix "-" -}}
{{- $existingSecret := default .Values.clickhouse.existingSecret .Values.clickhouse.secretName -}}
{{- if and (not .Values.externalSecrets) $existingSecret -}}
    {{- $secretName = $existingSecret -}}
{{- end -}}
{{- printf "%s" (tpl $secretName $) -}}
{{- end -}}

{{- define "scope-api.clickhouse.usernameKey" -}}
{{- default "clickhouseUser" .Values.clickhouse.usernameKey -}}
{{- end -}}

{{- define "scope-api.clickhouse.passwordKey" -}}
{{- default "clickhousePass" .Values.clickhouse.passwordKey -}}
{{- end -}}

{{/*
Return the secret containing the neo4j secrets
*/}}
{{- define "scope-api.neo4j.secretName" -}}
{{- $secretName := printf "%s-neo4j-secret" (include "scope-api.name" .) | trunc 63 | trimSuffix "-" -}}
{{- $existingSecret := default .Values.neo4j.existingSecret .Values.neo4j.secretName -}}
{{- if and (not .Values.externalSecrets) $existingSecret -}}
    {{- $secretName = $existingSecret -}}
{{- end -}}
{{- printf "%s" (tpl $secretName $) -}}
{{- end -}}

{{- define "scope-api.neo4j.authKey" -}}
{{- default "neo4jAuth" .Values.neo4j.authKey -}}
{{- end -}}

{{/*
Resolve the ClickHouse host for the API.
Precedence:
1) .Values.clickhouse.host
2) .Values.global.clickhouse.host
3) <baseName>-clickhouse
*/}}
{{- define "scope-api.clickhouse.host" -}}
{{- $global := get .Values "global" | default dict -}}
{{- $globalClickhouse := get $global "clickhouse" | default dict -}}
{{- $baseName := get $global "baseName" | default "scope" -}}
{{- $defaultHost := printf "%s-clickhouse" $baseName -}}
{{- $globalHost := get $globalClickhouse "host" | default "" -}}
{{- $host := .Values.clickhouse.host | default $globalHost | default $defaultHost -}}
{{- $host -}}
{{- end -}}

{{/*
Resolve the ClickHouse port for the API.
Precedence:
1) .Values.clickhouse.port
2) .Values.global.clickhouse.port (or .Values.global.clickhouse.httpPort)
3) 8123
*/}}
{{- define "scope-api.clickhouse.port" -}}
{{- $global := get .Values "global" | default dict -}}
{{- $globalClickhouse := get $global "clickhouse" | default dict -}}
{{- $defaultPort := "8123" -}}
{{- $globalPort := get $globalClickhouse "port" | default (get $globalClickhouse "httpPort" | default "") -}}
{{- $port := .Values.clickhouse.port | default $globalPort | default $defaultPort -}}
{{- $port -}}
{{- end -}}

{{/*
Resolve the Neo4j host for the API.
Precedence:
1) .Values.neo4j.host
2) .Values.global.neo4j.host
3) <baseName>-neo4j
*/}}
{{- define "scope-api.neo4j.host" -}}
{{- $global := get .Values "global" | default dict -}}
{{- $globalNeo4j := get $global "neo4j" | default dict -}}
{{- $baseName := get $global "baseName" | default "scope" -}}
{{- $defaultHost := printf "%s-neo4j" $baseName -}}
{{- $globalHost := get $globalNeo4j "host" | default "" -}}
{{- $host := .Values.neo4j.host | default $globalHost | default $defaultHost -}}
{{- $host -}}
{{- end -}}

{{/*
Resolve the Neo4j port for the API.
Precedence:
1) .Values.neo4j.port
2) .Values.global.neo4j.port
3) 7687
*/}}
{{- define "scope-api.neo4j.port" -}}
{{- $global := get .Values "global" | default dict -}}
{{- $globalNeo4j := get $global "neo4j" | default dict -}}
{{- $defaultPort := "7687" -}}
{{- $globalPort := get $globalNeo4j "port" | default "" -}}
{{- $port := .Values.neo4j.port | default $globalPort | default $defaultPort -}}
{{- $port -}}
{{- end -}}

{{/*
Resolve the internal OTLP endpoint used by the API.
*/}}
{{- define "scope-api.otelEndpoint" -}}
{{- $global := get .Values "global" | default dict -}}
{{- $baseName := get $global "baseName" | default "scope" -}}
{{- printf "http://%s-otel-collector:4318" $baseName -}}
{{- end -}}

{{/*
Return the secret containing the iam secrets
*/}}
{{- define "scope-api.iam.secretName" -}}
{{- $secretName := printf "%s-iam-secret" (include "scope-api.name" .) | trunc 63 | trimSuffix "-" -}}
{{- if and (not .Values.externalSecrets) .Values.iam.secretName -}}
    {{- $secretName = .Values.iam.secretName -}}
{{- end -}}
{{- printf "%s" (tpl $secretName $) -}}
{{- end -}}

{{/*
Return the secret containing the tls certificate secrets
*/}}
{{- define "scope-api.TLSCertificate.secretName" -}}
{{- $secretName := printf "%s-tls-secret" (include "scope-api.name" .) | trunc 63 | trimSuffix "-" -}}
{{- if and (not .Values.externalSecrets) .Values.TLSCertificate.secretName -}}
    {{- $secretName = .Values.TLSCertificate.secretName -}}
{{- end -}}
{{- printf "%s" (tpl $secretName $) -}}
{{- end -}}

{{/*
Validate credential sources. Only two modes are supported, and neither ever puts a
plaintext credential in the ConfigMap:
  * Vault mode (syncSecretsFromVault.enabled=true): the External Secrets Operator
    materialises the backing Secrets.
  * BYO mode (enabled=false): each backend must point at a pre-existing Secret via
    existingSecret. Plaintext passwords in values are not accepted.
*/}}
{{- define "scope-api.validate" -}}
{{- if not .Values.syncSecretsFromVault.enabled -}}
    {{- if not .Values.clickhouse.existingSecret -}}
        {{- fail "scope-api: clickhouse.existingSecret must be set when syncSecretsFromVault.enabled=false (plaintext credentials are not supported)" -}}
    {{- end -}}
    {{- if not .Values.neo4j.existingSecret -}}
        {{- fail "scope-api: neo4j.existingSecret must be set when syncSecretsFromVault.enabled=false (plaintext credentials are not supported)" -}}
    {{- end -}}
{{- end -}}
{{- end -}}
