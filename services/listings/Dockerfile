# The jar is built and tested by the workflow (./gradlew build) before this runs.
FROM eclipse-temurin:21.0.13_9-jre-noble
# Create the directory first: ADD --chmod would give the directory it creates mode 644 too, and user
# 10001 could not enter it.
RUN mkdir /opt/otel && chmod 755 /opt/otel
ADD --chmod=644 --checksum=sha256:e43a270df53c6f423e0ab157be9c27ccc01d5dc3232e162fe54777573db2f149 \
    https://repo1.maven.org/maven2/io/opentelemetry/javaagent/opentelemetry-javaagent/2.33.1/opentelemetry-javaagent-2.33.1.jar \
    /opt/otel/opentelemetry-javaagent.jar
COPY build/libs/listings.jar /app/listings.jar
USER 10001:10001
EXPOSE 8080
# The agent starts here, not through JAVA_TOOL_OPTIONS: the manifests use that variable for the heap.
ENTRYPOINT ["java", "-javaagent:/opt/otel/opentelemetry-javaagent.jar", "-jar", "/app/listings.jar"]
