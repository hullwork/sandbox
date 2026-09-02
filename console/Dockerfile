FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.31-alpine3.24@sha256:aa8c9087d36d93e9d650c5365f883b421e8214aedbad24ade52b844c583358f1
# Temporary, until the base image is rebuilt: this digest ships libexpat
# 2.8.3-r0 (CVE-2026-66046, CVE-2026-76641) while Alpine 3.24 already carries
# the fixed 2.8.4-r0, so the image gate would refuse it. Remove the two lines
# below once a newer base digest passes `trivy image` on its own; they are
# the one place this build reaches the package index after the FROM.
USER root
RUN apk upgrade --no-cache libexpat
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY entrypoint.sh /usr/local/bin/sandbox-console-entrypoint
COPY --from=build /app/dist /usr/share/nginx/html
USER 101:101
ENTRYPOINT ["/usr/local/bin/sandbox-console-entrypoint"]
CMD ["/usr/sbin/nginx", "-c", "/tmp/sandbox-console-nginx.conf", "-g", "daemon off;"]
EXPOSE 8080
