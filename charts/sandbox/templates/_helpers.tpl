{{- define "sandbox.image" -}}
{{- $image := index . 0 -}}
{{- if $image.digest -}}
{{ printf "%s@%s" $image.repository $image.digest }}
{{- else -}}
{{ printf "%s:%s" $image.repository $image.tag }}
{{- end -}}
{{- end -}}

{{- define "sandbox.systemScheduling" -}}
{{- with .Values.scheduling.system.nodeSelector }}
nodeSelector:
{{ toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.scheduling.system.tolerations }}
tolerations:
{{ toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
