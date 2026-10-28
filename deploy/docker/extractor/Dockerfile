FROM ghcr.io/codelibs/fess:15.8.0@sha256:442a58831e87aae6ae22ef60e0536ececbda553bd5f5e8e5dc98cc022578dc75 AS fess

FROM eclipse-temurin:21-jdk-jammy@sha256:4e61d0b3471137e239397789661f6adeaf8db9adcfae45916b22f628c8a32dc0 AS compiler
COPY --from=fess /usr/share/fess/app/WEB-INF/lib/ /fess/lib/
COPY deploy/docker/extractor/Main.java /source/Main.java
RUN javac --release 21 -encoding UTF-8 -cp '/fess/lib/*' -d /classes /source/Main.java

FROM fess
USER root
COPY --from=compiler --chown=root:root /classes/ /opt/depositarai-extractor/classes/
COPY --chown=root:root deploy/docker/extractor/log4j2.xml /opt/depositarai-extractor/log4j2.xml
RUN chmod -R a=rX /opt/depositarai-extractor
USER 1001:1001
ENTRYPOINT ["/opt/java/openjdk/bin/java", "-Xmx1400m", "-Djava.io.tmpdir=/tmp", "-Duser.home=/tmp", "-Dlog4j2.configurationFile=/opt/depositarai-extractor/log4j2.xml", "-cp", "/opt/depositarai-extractor/classes:/usr/share/fess/app/WEB-INF/lib/*", "org.depositarai.extractor.Main"]
