# Dashboard de memex servido estático por nginx (build de producción, no el dev server de Vite).
#
# Por qué contenedor y no `npm run dev`: en dev el dashboard vivía como un proceso suelto que moría
# al cerrar la terminal/sesión. Acá arranca con el resto del compose y `restart: unless-stopped` lo
# vuelve a levantar tras un reboot, igual que el API. nginx sirve el SPA y proxya `/api/*` al
# contenedor `api` quitando el prefijo (mismo contrato que el proxy de Vite: el browser pega a
# mismo-origen, sin CORS). Contexto de build = ./frontend.

# --- build ---------------------------------------------------------------------------------------
FROM node:22-alpine AS build
WORKDIR /app
# Deps primero (capa cacheable): package.json/lock cambian poco.
COPY package.json package-lock.json ./
RUN npm ci
COPY . ./
RUN npm run build

# --- serve ---------------------------------------------------------------------------------------
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
