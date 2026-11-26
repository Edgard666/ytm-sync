# Images épinglées par version ET par empreinte : une construction locale donne toujours le
# même Java et le même JOAL. La construction GitHub (.github/workflows/image.yml) remplace ces
# trois valeurs chaque semaine par les dernières versions, puis ne publie qu'après les tests.
ARG JOAL_IMAGE=anthonyraymond/joal:2.1.37@sha256:fc942567c5649b29377e50aa0ca934e4750534d02b4ecf5a0eda3984630f35d7
ARG JDK_IMAGE=eclipse-temurin:21-jdk-jammy@sha256:b2711c65970c1227197c29c46e669bfdbb137524e9da0e664703a140ec25a040
ARG JRE_IMAGE=eclipse-temurin:21-jre-jammy@sha256:a8bff88f760e87e0c8844f8461f807c203fd5e8f78c951c843024647e965eaf5
FROM ${JOAL_IMAGE} AS upstream
FROM ${JDK_IMAGE} AS build
WORKDIR /build
COPY --from=upstream /joal/joal.jar .
COPY telemetry/*.java ./
RUN jar xf joal.jar BOOT-INF && javac --release 17 -proc:none -cp 'BOOT-INF/classes:BOOT-INF/lib/*' -d BOOT-INF/classes UploadTelemetry.java && java --add-exports java.base/jdk.internal.org.objectweb.asm=ALL-UNNAMED RecordTrackerHost.java BOOT-INF/classes/org/araymond/joal/core/ttorrent/client/announcer/tracker/TrackerClientUriProvider.class && jar uf joal.jar BOOT-INF/classes/org/araymond/joal/core/ttorrent/client/announcer/tracker/TrackerClientUriProvider.class BOOT-INF/classes/org/araymond/joal/telemetry/UploadTelemetry.class
FROM ${JRE_IMAGE}
ARG JOAL_VERSION=2.1.37
# Empreinte des entrées de la construction (code, JOAL, Java, profils) : la construction
# GitHub ne republie pas une image identique, donc pas de fausse « mise à jour » dans Unraid.
ARG BUILD_INPUTS=local
# Lets update-joal.sh remove this application's old images, and only them.
LABEL app.joal-smart-controller="1" app.joal-smart-controller.joal="${JOAL_VERSION}" \
      app.joal-smart-controller.inputs="${BUILD_INPUTS}"
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/joal.jar /opt/joal/joal.jar
# Profils .client de JOAL : ajoutés au démarrage dans /data/clients s'ils y manquent (jamais écrasés).
COPY clients/ /opt/joal/clients/
WORKDIR /app
COPY app/ /app/
COPY config.example.json /app/config.example.json
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Tolère une extraction Windows (CRLF, bit exécutable perdu).
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod 0755 /usr/local/bin/docker-entrypoint.sh
# Normalize only files shipped in the application image, independently of SMB/ZIP modes.
RUN find /app -type d -exec chmod 0755 {} + && find /app -type f -exec chmod 0644 {} +
USER 99:100
RUN python3 -B -c "import main; from pathlib import Path; Path('static/login.html').read_bytes(); Path('static/index.html').read_bytes(); Path('config.example.json').read_bytes()"
USER root
ENV PYTHONUNBUFFERED=1 PORT=8788 PUID=99 PGID=100 HOME=/tmp JOAL_VERSION=${JOAL_VERSION}
EXPOSE 8788
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8788/healthz', timeout=4)"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python3", "-u", "/app/main.py"]
