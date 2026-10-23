{{/*
Expand the name of the chart.
*/}}
{{- define "scope.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "scope.fullname" -}}
{{- $global := get .Values "global" | default dict -}}
{{- $baseName := get $global "baseName" -}}
{{- if $baseName }}
{{- $baseName | trunc 63 | trimSuffix "-" }}
{{- else if .Values.fullnameOverride }}
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
{{- define "scope.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "scope.labels" -}}
helm.sh/chart: {{ include "scope.chart" . }}
{{ include "scope.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "scope.selectorLabels" -}}
app.kubernetes.io/name: {{ include "scope.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "scope.secretName" -}}
{{- if .Values.secrets.llm.existingSecret -}}
{{- .Values.secrets.llm.existingSecret -}}
{{- else -}}
{{- default (printf "%s-llm-secret" (include "scope.fullname" .)) .Values.secrets.llm.name -}}
{{- end -}}
{{- end }}

{{- define "scope.workerSecretName" -}}
{{- $root := .root -}}
{{- $name := .name -}}
{{- printf "%s-worker-%s-secret" (include "scope.fullname" $root) $name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "scope.workerDeploymentName" -}}
{{- $root := .root -}}
{{- $name := .name -}}
{{- printf "%s-worker-%s" (include "scope.fullname" $root) $name | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{- define "scope.workerEnvConfigMapName" -}}
{{- printf "%s-worker-env-extra" (include "scope.fullname" .) -}}
{{- end }}

{{- define "scope.mceConfigMapName" -}}
{{- printf "%s-mce-config" (include "scope.fullname" .) -}}
{{- end }}

{{- define "scope.statefulEvalConfigMapName" -}}
{{- printf "%s-stateful-eval-config" (include "scope.fullname" .) -}}
{{- end }}

{{- define "scope.cognitiveFailureConfigMapName" -}}
{{- printf "%s-cognitive-failure-config" (include "scope.fullname" .) -}}
{{- end }}

{{/*
Output queues of the embedding worker: the configured ones, plus the input queues of the optional
branch workers (cognitive-failure, cognition-protocols) that are deployed, so that enabling one is enough to feed it.
*/}}
{{- define "scope.embeddingOutputQueues" -}}
{{- $queues := list -}}
{{- if kindIs "slice" .Values.workers.embedding.outputQueue -}}
{{- $queues = .Values.workers.embedding.outputQueue -}}
{{- else if .Values.workers.embedding.outputQueue -}}
{{- $queues = splitList "," (toString .Values.workers.embedding.outputQueue) -}}
{{- end -}}
{{- range $branch := list "cognitive-failure" "cognition-protocols" -}}
{{- $branchWorker := index $.Values.workers $branch -}}
{{- if and $.Values.workers.enabled $branchWorker.enabled -}}
{{- $queues = append $queues $branchWorker.inputQueue -}}
{{- end -}}
{{- end -}}
{{- join "," (uniq $queues) -}}
{{- end }}

{{- define "scope.otelCollectorName" -}}
{{- printf "%s-otel-collector" (include "scope.fullname" .) -}}
{{- end }}

{{- define "scope.apiBaseUrl" -}}
{{- $apiValues := .Values.api | default dict -}}
{{- $externalConfig := get $apiValues "external" | default dict -}}
{{- $externalApi := get $externalConfig "baseUrl" | default "" -}}
{{- if $externalApi -}}
{{- $externalApi -}}
{{- else -}}
{{- $apiPort := int .Values.api.api.service.port -}}
{{- printf "http://%s-api.%s.svc.cluster.local:%d" (include "scope.fullname" .) .Release.Namespace $apiPort -}}
{{- end -}}
{{- end -}}

{{- define "scope.clickhouseName" -}}
{{- printf "%s-clickhouse" (include "scope.fullname" .) -}}
{{- end }}

{{- define "scope.clickhouseHost" -}}
{{- $clickhouseValues := .Values.clickhouse | default dict -}}
{{- $externalConfig := get $clickhouseValues "external" | default dict -}}
{{- $externalHost := get $externalConfig "host" | default "" -}}
{{- $global := .Values.global | default dict -}}
{{- $globalClickhouse := get $global "clickhouse" | default dict -}}
{{- $globalHost := get $globalClickhouse "host" | default "" -}}
{{- if $externalHost -}}
{{- $externalHost -}}
{{- else if $globalHost -}}
{{- $globalHost -}}
{{- else -}}
{{- printf "%s.%s.svc.cluster.local" (include "scope.clickhouseName" .) .Release.Namespace -}}
{{- end -}}
{{- end }}

{{- define "scope.clickhouseHttpPort" -}}
{{- $clickhouseValues := .Values.clickhouse | default dict -}}
{{- $externalConfig := get $clickhouseValues "external" | default dict -}}
{{- $externalPort := get $externalConfig "httpPort" | default "" -}}
{{- $global := .Values.global | default dict -}}
{{- $globalClickhouse := get $global "clickhouse" | default dict -}}
{{- $globalPort := get $globalClickhouse "port" | default (get $globalClickhouse "httpPort" | default "") -}}
{{- if $externalPort -}}
{{- $externalPort -}}
{{- else if $globalPort -}}
{{- $globalPort -}}
{{- else -}}
{{- .Values.clickhouse.service.ports.http -}}
{{- end -}}
{{- end }}

{{- define "scope.clickhouseTcpPort" -}}
{{- $clickhouseValues := .Values.clickhouse | default dict -}}
{{- $externalConfig := get $clickhouseValues "external" | default dict -}}
{{- $externalPort := get $externalConfig "tcpPort" | default "" -}}
{{- $global := .Values.global | default dict -}}
{{- $globalClickhouse := get $global "clickhouse" | default dict -}}
{{- $globalPort := get $globalClickhouse "tcpPort" | default "" -}}
{{- if $externalPort -}}
{{- $externalPort -}}
{{- else if $globalPort -}}
{{- $globalPort -}}
{{- else -}}
{{- .Values.clickhouse.service.ports.tcp -}}
{{- end -}}
{{- end }}

{{- define "scope.rabbitmqName" -}}
{{- printf "%s-rabbitmq" (include "scope.fullname" .) -}}
{{- end }}

{{- define "scope.rabbitmqHost" -}}
{{- $rabbitmqValues := .Values.rabbitmq | default dict -}}
{{- $externalConfig := get $rabbitmqValues "external" | default dict -}}
{{- $externalHost := get $externalConfig "host" | default "" -}}
{{- $global := .Values.global | default dict -}}
{{- $globalRabbitmq := get $global "rabbitmq" | default dict -}}
{{- $globalHost := get $globalRabbitmq "host" | default "" -}}
{{- if $externalHost -}}
{{- $externalHost -}}
{{- else if $globalHost -}}
{{- $globalHost -}}
{{- else -}}
{{- printf "%s.%s.svc.cluster.local" (include "scope.rabbitmqName" .) .Release.Namespace -}}
{{- end -}}
{{- end }}

{{- define "scope.rabbitmqPort" -}}
{{- $rabbitmqValues := .Values.rabbitmq | default dict -}}
{{- $externalConfig := get $rabbitmqValues "external" | default dict -}}
{{- $externalPort := get $externalConfig "port" | default "" -}}
{{- $global := .Values.global | default dict -}}
{{- $globalRabbitmq := get $global "rabbitmq" | default dict -}}
{{- $globalPort := get $globalRabbitmq "port" | default "" -}}
{{- if $externalPort -}}
{{- $externalPort -}}
{{- else if $globalPort -}}
{{- $globalPort -}}
{{- else -}}
{{- .Values.rabbitmq.service.ports.amqp -}}
{{- end -}}
{{- end }}

{{- define "scope.neo4jName" -}}
{{- printf "%s-neo4j" (include "scope.fullname" .) -}}
{{- end }}

{{- define "scope.neo4jHost" -}}
{{- $neo4jValues := .Values.neo4j | default dict -}}
{{- $externalConfig := get $neo4jValues "external" | default dict -}}
{{- $externalHost := get $externalConfig "host" | default "" -}}
{{- $global := .Values.global | default dict -}}
{{- $globalNeo4j := get $global "neo4j" | default dict -}}
{{- $globalHost := get $globalNeo4j "host" | default "" -}}
{{- if $externalHost -}}
{{- $externalHost -}}
{{- else if $globalHost -}}
{{- $globalHost -}}
{{- else -}}
{{- printf "%s.%s.svc.cluster.local" (include "scope.neo4jName" .) .Release.Namespace -}}
{{- end -}}
{{- end }}

{{- define "scope.neo4jPort" -}}
{{- $neo4jValues := .Values.neo4j | default dict -}}
{{- $externalConfig := get $neo4jValues "external" | default dict -}}
{{- $externalPort := get $externalConfig "port" | default "" -}}
{{- $global := .Values.global | default dict -}}
{{- $globalNeo4j := get $global "neo4j" | default dict -}}
{{- $globalPort := get $globalNeo4j "port" | default "" -}}
{{- if $externalPort -}}
{{- $externalPort -}}
{{- else if $globalPort -}}
{{- $globalPort -}}
{{- else -}}
{{- $neo4jPort := 7687 -}}
{{- if and .Values.neo4j.services .Values.neo4j.services.default .Values.neo4j.services.default.ports .Values.neo4j.services.default.ports.bolt (hasKey .Values.neo4j.services.default.ports.bolt "port") -}}
{{- $neo4jPort = int .Values.neo4j.services.default.ports.bolt.port -}}
{{- end -}}
{{- $neo4jPort -}}
{{- end -}}
{{- end }}

{{- define "scope.clickhouseSecretName" -}}
{{- default .Values.clickhouse.auth.secretName .Values.clickhouse.auth.existingSecret -}}
{{- end }}

{{- define "scope.clickhouseUsersSecretName" -}}
{{- printf "%s-clickhouse-users" (include "scope.fullname" .) -}}
{{- end }}

{{- define "scope.clickhouseWorkerEnv" -}}
- name: CLICKHOUSE_HOST
  value: {{ include "scope.clickhouseHost" . | quote }}
- name: CLICKHOUSE_PORT
  value: {{ include "scope.clickhouseHttpPort" . | quote }}
- name: CLICKHOUSE_USERNAME
  valueFrom:
    secretKeyRef:
      name: {{ include "scope.clickhouseSecretName" . }}
      key: {{ .Values.clickhouse.auth.usernameKey }}
- name: CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "scope.clickhouseSecretName" . }}
      key: {{ .Values.clickhouse.auth.passwordKey }}
- name: CLICKHOUSE_DATABASE
  value: {{ .Values.clickhouse.database | quote }}
{{- end }}

{{- define "scope.rabbitmqSecretName" -}}
{{- default .Values.rabbitmq.auth.secretName .Values.rabbitmq.auth.existingSecret -}}
{{- end }}

{{- define "scope.rabbitmqUsernameKey" -}}
{{- default "username" .Values.rabbitmq.auth.usernameKey -}}
{{- end }}

{{- define "scope.rabbitmqPasswordKey" -}}
{{- default "password" .Values.rabbitmq.auth.passwordKey -}}
{{- end }}

{{- define "scope.rabbitmqWorkerEnv" -}}
- name: RABBITMQ_HOST
  value: {{ include "scope.rabbitmqHost" . | quote }}
- name: RABBITMQ_PORT
  value: {{ include "scope.rabbitmqPort" . | quote }}
- name: RABBITMQ_USER
  valueFrom:
    secretKeyRef:
      name: {{ include "scope.rabbitmqSecretName" . }}
      key: {{ include "scope.rabbitmqUsernameKey" . }}
- name: RABBITMQ_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "scope.rabbitmqSecretName" . }}
      key: {{ include "scope.rabbitmqPasswordKey" . }}
- name: RABBITMQ_VHOST
  value: {{ default "/" .Values.rabbitmq.auth.vhost | quote }}
{{- end }}

{{- define "scope.neo4jWorkerEnv" -}}
- name: NEO4J_HOST
  value: {{ include "scope.neo4jHost" . | quote }}
- name: NEO4J_PORT
  value: {{ include "scope.neo4jPort" . | quote }}
- name: NEO4J_URI
  value: {{ printf "bolt://%s:%s" (include "scope.neo4jHost" .) (include "scope.neo4jPort" .) | quote }}
- name: NEO4J_AUTH
  valueFrom:
    secretKeyRef:
      name: {{ include "scope.neo4jAuthSecretName" . }}
      key: {{ .Values.neo4j.neo4j.authKey }}
- name: NEO4J_DB
  value: {{ default "neo4j" .Values.neo4j.database | quote }}
{{- end }}

{{- define "scope.otelEndpoint" -}}
{{- $otelHttpPort := 4318 -}}
{{- if and .Values.otelCollector.ports (hasKey .Values.otelCollector.ports "otlp-http") -}}
{{- $otelHttpPort = int (index (index .Values.otelCollector.ports "otlp-http") "servicePort") -}}
{{- end -}}
{{- printf "http://%s.%s.svc.cluster.local:%d" (include "scope.otelCollectorName" .) .Release.Namespace $otelHttpPort -}}
{{- end }}

{{- define "scope.listOrStringEnvValue" -}}
{{- if kindIs "slice" . -}}
{{ join "," . }}
{{- else -}}
{{ . }}
{{- end -}}
{{- end }}

{{- define "scope.neo4jSecretName" -}}
{{- if .Values.neo4j.neo4j.passwordFromSecret -}}
{{- .Values.neo4j.neo4j.passwordFromSecret -}}
{{- else -}}
{{- .Values.neo4j.neo4j.secretName -}}
{{- end -}}
{{- end }}

{{- define "scope.neo4jAuthSecretName" -}}
{{- if .Values.neo4j.neo4j.passwordFromSecret -}}
{{- .Values.neo4j.neo4j.passwordFromSecret -}}
{{- else -}}
{{- printf "%s-auth" (include "scope.neo4jName" .) -}}
{{- end -}}
{{- end }}

{{- define "scope.neo4jUsernameKey" -}}
{{- .Values.neo4j.neo4j.usernameKey -}}
{{- end }}

{{- define "scope.neo4jPasswordKey" -}}
{{- .Values.neo4j.neo4j.passwordKey -}}
{{- end }}

{{- define "scope.clickhouseUsername" -}}
{{- .Values.clickhouse.auth.username -}}
{{- end }}

{{- define "scope.clickhousePassword" -}}
{{- .Values.clickhouse.auth.password -}}
{{- end }}

{{- define "scope.workerInitContainers.waitDependencies" -}}
{{- $root := .root -}}
{{- $includeClickhouse := default false .includeClickhouse -}}
{{- $waitConfig := $root.Values.workers.startupWait -}}
{{- if $waitConfig.enabled -}}
{{- $waitImage := $waitConfig.image -}}
{{- if $waitConfig.rabbitmq.enabled }}
- name: wait-for-rabbitmq
  image: {{ printf "%s:%s" $waitImage.repository $waitImage.tag | quote }}
  imagePullPolicy: {{ $waitImage.pullPolicy }}
  command:
    - sh
    - -c
    - |
      echo "Waiting for RabbitMQ at {{ include "scope.rabbitmqHost" $root }}:{{ include "scope.rabbitmqPort" $root }}"
      start=$(date +%s)
      until nc -z -w {{ $waitConfig.connectTimeoutSeconds }} {{ include "scope.rabbitmqHost" $root }} {{ include "scope.rabbitmqPort" $root }}; do
        now=$(date +%s)
        elapsed=$((now-start))
        if [ "$elapsed" -ge {{ $waitConfig.maxWaitSeconds }} ]; then
          echo "Timed out waiting for RabbitMQ after {{ $waitConfig.maxWaitSeconds }} seconds"
          exit 1
        fi
        echo "RabbitMQ not ready yet..."
        sleep {{ $waitConfig.retryIntervalSeconds }}
      done
{{- end }}
{{- if $waitConfig.neo4j.enabled }}
- name: wait-for-neo4j
  image: {{ printf "%s:%s" $waitImage.repository $waitImage.tag | quote }}
  imagePullPolicy: {{ $waitImage.pullPolicy }}
  command:
    - sh
    - -c
    - |
      echo "Waiting for Neo4j HTTP readiness at {{ include "scope.neo4jHost" $root }}:7474"
      start=$(date +%s)
      until wget -q -T {{ $waitConfig.connectTimeoutSeconds }} -O /dev/null "http://{{ include "scope.neo4jHost" $root }}:7474/"; do
        now=$(date +%s)
        elapsed=$((now-start))
        if [ "$elapsed" -ge {{ $waitConfig.maxWaitSeconds }} ]; then
          echo "Timed out waiting for Neo4j after {{ $waitConfig.maxWaitSeconds }} seconds"
          exit 1
        fi
        echo "Neo4j not ready yet..."
        sleep {{ $waitConfig.retryIntervalSeconds }}
      done
      echo "Neo4j is ready"
{{- end }}
{{- if and $includeClickhouse $waitConfig.clickhouse.enabled }}
- name: wait-for-clickhouse
  image: {{ printf "%s:%s" $waitImage.repository $waitImage.tag | quote }}
  imagePullPolicy: {{ $waitImage.pullPolicy }}
  command:
    - sh
    - -c
    - |
      echo "Waiting for ClickHouse at {{ include "scope.clickhouseHost" $root }}:{{ include "scope.clickhouseHttpPort" $root }}"
      start=$(date +%s)
      until nc -z -w {{ $waitConfig.connectTimeoutSeconds }} {{ include "scope.clickhouseHost" $root }} {{ include "scope.clickhouseHttpPort" $root }}; do
        now=$(date +%s)
        elapsed=$((now-start))
        if [ "$elapsed" -ge {{ $waitConfig.maxWaitSeconds }} ]; then
          echo "Timed out waiting for ClickHouse after {{ $waitConfig.maxWaitSeconds }} seconds"
          exit 1
        fi
        echo "ClickHouse not ready yet..."
        sleep {{ $waitConfig.retryIntervalSeconds }}
      done
{{- end }}
{{- end }}
{{- end }}

