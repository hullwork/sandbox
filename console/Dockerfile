FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.31-alpine3.24@sha256:d9083fe47768377ef55dedafd67d4da7c2f2bc2bece7554954f29359deb0dce9
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY entrypoint.sh /usr/local/bin/sandbox-console-entrypoint
COPY --from=build /app/dist /usr/share/nginx/html
USER 101:101
ENTRYPOINT ["/usr/local/bin/sandbox-console-entrypoint"]
CMD ["/usr/sbin/nginx", "-c", "/tmp/sandbox-console-nginx.conf", "-g", "daemon off;"]
EXPOSE 8080
