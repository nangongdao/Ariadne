{{/*
Ariadne 通用模板辅助函数。
*/}}

{{/* 全限定镜像地址 */}}
{{- define "ariadne.image" -}}
{{- $repo := .Values.global.image.repository -}}
{{- $tag := .Values.global.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}

{{/* 组件全名：ariadne-{component} */}}
{{- define "ariadne.fullname" -}}
{{- printf "%s-%s" .Release.Name .componentName -}}
{{- end -}}

{{/* 通用标签 */}}
{{- define "ariadne.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/name: ariadne
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .componentName }}
app.kubernetes.io/component: {{ . }}
{{- end }}
{{- end -}}

{{/* 通用选择器标签（不含 component，用于 Service 匹配） */}}
{{- define "ariadne.selectorLabels" -}}
app.kubernetes.io/name: ariadne
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .componentName }}
{{- end -}}

{{/* 存储连接环境变量（api / worker 共享，不含 Postgres owner 凭据）

   变量名必须与 src/ariadne/config.py 的 env_prefix + 字段名逐字对应。
   pydantic-settings 对无匹配字段的变量静默忽略，所以名字写错不报错、
   只是回落默认值（localhost），表现为"连不上数据库"而非"配置写错了"。
   曾经的 ARIADNE_CH_URL / ARIADNE_PG_URL / ARIADNE_S3_* 都属于这一类。
*/}}
{{- define "ariadne.storageEnv" -}}
- name: ARIADNE_CH_HOST
  value: {{ .Values.global.clickhouse.host | quote }}
- name: ARIADNE_CH_PORT
  value: {{ .Values.global.clickhouse.port | quote }}
- name: ARIADNE_CH_DATABASE
  value: {{ .Values.global.clickhouse.database | quote }}
{{- if .Values.global.clickhouse.existingSecret }}
- name: ARIADNE_CH_USER
  valueFrom:
    secretKeyRef:
      name: {{ .Values.global.clickhouse.existingSecret }}
      key: {{ .Values.global.clickhouse.usernameKey }}
- name: ARIADNE_CH_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.global.clickhouse.existingSecret }}
      key: {{ .Values.global.clickhouse.passwordKey }}
{{- else }}
- name: ARIADNE_CH_USER
  value: {{ .Values.global.clickhouse.user | quote }}
{{- end }}
- name: ARIADNE_PG_HOST
  value: {{ .Values.global.postgres.host | quote }}
- name: ARIADNE_PG_PORT
  value: {{ .Values.global.postgres.port | quote }}
- name: ARIADNE_PG_DATABASE
  value: {{ .Values.global.postgres.database | quote }}
{{- /* 应用连接走非 owner 角色，否则 owner 的 BYPASSRLS 会让租户策略形同虚设。
       appUser 必须非空：留空时 app_dsn() 回落 owner，而 PostgresSettings 的
       owner 默认值恰好是 ariadne/ariadne —— 与本 chart 的默认 owner 一致，
       所以回落会静默连上并拿到 owner 权限，不会报错。
       靠 tests/test_helm_chart_contract.py 的 test_app_user_is_configured 兜。 */}}
- name: ARIADNE_PG_APP_USER
  value: {{ .Values.global.postgres.appUser | quote }}
{{- if .Values.global.postgres.existingSecret }}
- name: ARIADNE_PG_APP_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.global.postgres.existingSecret }}
      key: {{ .Values.global.postgres.appPasswordKey }}
{{- else }}
- name: ARIADNE_PG_APP_PASSWORD
  value: {{ .Values.global.postgres.appPassword | quote }}
{{- end }}
- name: ARIADNE_REDIS_URL
  value: {{ .Values.global.redis.url | quote }}
{{- if .Values.global.redis.existingSecret }}
- name: ARIADNE_REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.global.redis.existingSecret }}
      key: {{ .Values.global.redis.passwordKey }}
{{- end }}
{{- include "ariadne.payloadEnv" . }}
{{- end -}}

{{/* Payload 对象存储。

   store_backend 必须显式给：默认 local 会把 payload 写进容器本地盘，
   随 Pod 重启丢失，而 bucket 配了也不会被用到。
   endpoint / region 用 AWS 标准变量名 —— S3ObjectStore 是裸
   boto3.client("s3")，botocore 自己读这两个，不经 ARIADNE_ 前缀。
*/}}
{{- define "ariadne.payloadEnv" -}}
{{- if .Values.global.s3.bucket }}
- name: ARIADNE_PAYLOAD_STORE_BACKEND
  value: "s3"
- name: ARIADNE_PAYLOAD_S3_BUCKET
  value: {{ .Values.global.s3.bucket | quote }}
- name: ARIADNE_PAYLOAD_S3_PREFIX
  value: {{ .Values.global.s3.prefix | quote }}
{{- with .Values.global.s3.endpoint }}
- name: AWS_ENDPOINT_URL_S3
  value: {{ . | quote }}
{{- end }}
- name: AWS_DEFAULT_REGION
  value: {{ .Values.global.s3.region | quote }}
{{- with .Values.global.s3.existingSecret }}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: accessKeyId
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ . }}
      key: secretAccessKey
{{- end }}
{{- end }}
{{- end -}}

{{/* Postgres owner 凭据 —— 仅迁移 Job。

   Alembic 要建表、建角色、开 RLS 策略，这些是 owner 权限。
   常驻服务刻意拿不到这份凭据：拿得到就等于 RLS 可被绕过。
*/}}
{{- define "ariadne.pgOwnerEnv" -}}
- name: ARIADNE_PG_USER
  value: {{ .Values.global.postgres.user | quote }}
{{- if .Values.global.postgres.existingSecret }}
- name: ARIADNE_PG_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.global.postgres.existingSecret }}
      key: {{ .Values.global.postgres.passwordKey }}
{{- else }}
- name: ARIADNE_PG_PASSWORD
  value: {{ .Values.global.postgres.password | quote }}
{{- end }}
{{- end -}}
