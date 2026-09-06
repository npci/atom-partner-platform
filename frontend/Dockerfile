FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
# Deployment-supplied UI labels; unset -> the neutral defaults in src/strings.js.
# See the NPCI frontend Dockerfile for why this is a file and not a JSON string.
ARG LABEL_PROFILE=""

# Outbound-link host allowlist for safeUrl.js (Checkmarx #1/#2/#9 — DOM XSS and
# open redirect). Vite inlines import.meta.env at BUILD time, so this has to be
# a build arg: a runtime env var cannot reach an already-compiled bundle. Unset
# leaves only same-origin links valid, and GitLab MR links render as '#'.
ARG VITE_SAFE_REDIRECT_HOSTS=""
ENV VITE_SAFE_REDIRECT_HOSTS=$VITE_SAFE_REDIRECT_HOSTS

# GitLab origin used to build merge-request links in the browser.
#
# The API deliberately no longer returns a ready-made MR URL: Checkmarx traced
# that value into an <a href> under three queries (Client DOM XSS, Reflected
# XSS, Client DOM Open Redirect). The backend now sends {project_path, mr_iid}
# and src/utils/mrUrl.js assembles the address against THIS origin, so no
# server-supplied string reaches an href.
#
# Build arg for the same reason as the allowlist above — Vite inlines
# import.meta.env at build time. Unset means MR references render as plain text
# ("!42") instead of links, which is a clean degradation, not a failure.
# Example:  GITLAB_BASE_URL=https://gitlab.example.com
ARG VITE_GITLAB_BASE_URL=""
ENV VITE_GITLAB_BASE_URL=$VITE_GITLAB_BASE_URL

RUN if [ -n "$LABEL_PROFILE" ]; then \
      [ -f "$LABEL_PROFILE" ] || { echo "LABEL_PROFILE $LABEL_PROFILE not found"; exit 1; }; \
      VITE_LABEL_OVERRIDES="$(cat "$LABEL_PROFILE")" npm run build; \
    else npm run build; fi

FROM nginx:alpine
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/conf.d/default.conf.template
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
EXPOSE 80
ENTRYPOINT ["/docker-entrypoint.sh"]
