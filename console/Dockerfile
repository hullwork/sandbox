FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.31-alpine3.24@sha256:a6c3ec0c0d249d68b0682df854d4a9e222b90fb607dc3fcf2f1d2fcbc85d347e
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY entrypoint.sh /usr/local/bin/sandbox-console-entrypoint
COPY --from=build /app/dist /usr/share/nginx/html
USER 101:101
ENTRYPOINT ["/usr/local/bin/sandbox-console-entrypoint"]
CMD ["/usr/sbin/nginx", "-c", "/tmp/sandbox-console-nginx.conf", "-g", "daemon off;"]
EXPOSE 8080
