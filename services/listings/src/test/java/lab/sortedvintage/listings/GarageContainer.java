package lab.sortedvintage.listings;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.security.SecureRandom;
import java.time.Duration;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import org.testcontainers.containers.GenericContainer;
import org.testcontainers.containers.wait.strategy.Wait;
import org.testcontainers.utility.MountableFile;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/**
 * Garage, the lab's S3 store (research R19), set up the way gitops platform/garage/base/setup.py
 * sets up the lab: a one-node layout, an imported key and the bucket {@code photos}, through the
 * admin API v2. The image has no shell, so readiness is checked over HTTP only.
 */
final class GarageContainer extends GenericContainer<GarageContainer> {

    static final String IMAGE = "dxflrs/garage:v2.5.0";
    static final String BUCKET = "photos";
    static final String REGION = "us-east-1";
    private static final int S3_PORT = 3900;
    private static final int ADMIN_PORT = 3903;

    private static final SecureRandom RANDOM = new SecureRandom();
    private static final JsonMapper JSON = JsonMapper.builder().build();

    private final String adminToken = hex(32);
    private final String accessKeyId = "GK" + hex(12);
    private final String secretAccessKey = hex(32);
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

    GarageContainer() {
        super(IMAGE);
        withExposedPorts(S3_PORT, ADMIN_PORT);
        withEnv("GARAGE_RPC_SECRET", hex(32));
        withEnv("GARAGE_ADMIN_TOKEN", adminToken);
        withCopyFileToContainer(MountableFile.forClasspathResource("garage/garage.toml"), "/etc/garage.toml");
        // Any answer from /health means the admin API is listening; the layout comes after.
        waitingFor(Wait.forHttp("/health").forPort(ADMIN_PORT).forStatusCodeMatching(code -> code > 0)
                .withStartupTimeout(Duration.ofMinutes(2)));
    }

    @Override
    public void start() {
        super.start();
        try {
            setUpBucket();
        } catch (Exception e) {
            throw new IllegalStateException("Garage setup failed", e);
        }
    }

    String s3Endpoint() {
        return "http://" + getHost() + ":" + getMappedPort(S3_PORT);
    }

    String accessKeyId() {
        return accessKeyId;
    }

    String secretAccessKey() {
        return secretAccessKey;
    }

    private void setUpBucket() throws Exception {
        JsonNode status = retry(() -> call("GET", "/v2/GetClusterStatus", null));
        String node = status.get("nodes").get(0).get("id").asString();
        call("POST", "/v2/UpdateClusterLayout", Map.of("roles",
                List.of(Map.of("id", node, "zone", "test", "capacity", 1L << 30, "tags", List.of()))));
        int version = call("GET", "/v2/GetClusterLayout", null).get("version").asInt();
        call("POST", "/v2/ApplyClusterLayout", Map.of("version", version + 1));
        call("POST", "/v2/ImportKey", Map.of(
                "accessKeyId", accessKeyId, "secretAccessKey", secretAccessKey, "name", "listings-test"));
        // The layout takes a moment to apply; bucket creation is refused until it has.
        String bucketId = retry(() -> call("POST", "/v2/CreateBucket", Map.of("globalAlias", BUCKET)))
                .get("id").asString();
        call("POST", "/v2/AllowBucketKey", Map.of(
                "bucketId", bucketId,
                "accessKeyId", accessKeyId,
                "permissions", Map.of("read", true, "write", true, "owner", false)));
    }

    private JsonNode call(String method, String path, Object body) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(
                        URI.create("http://" + getHost() + ":" + getMappedPort(ADMIN_PORT) + path))
                .timeout(Duration.ofSeconds(10))
                .header("Authorization", "Bearer " + adminToken)
                .header("Content-Type", "application/json");
        request.method(method, body == null
                ? HttpRequest.BodyPublishers.noBody()
                : HttpRequest.BodyPublishers.ofString(JSON.writeValueAsString(body)));
        HttpResponse<String> response = http.send(request.build(), HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() / 100 != 2) {
            throw new IllegalStateException(method + " " + path + ": HTTP " + response.statusCode() + " "
                    + response.body());
        }
        return response.body().isBlank() ? JSON.createObjectNode() : JSON.readTree(response.body());
    }

    private interface Call {
        JsonNode run() throws Exception;
    }

    private static JsonNode retry(Call call) throws Exception {
        Exception last = null;
        for (int i = 0; i < 30; i++) {
            try {
                return call.run();
            } catch (Exception e) {
                last = e;
                Thread.sleep(1000);
            }
        }
        throw last;
    }

    private static String hex(int bytes) {
        byte[] b = new byte[bytes];
        RANDOM.nextBytes(b);
        return HexFormat.of().formatHex(b);
    }
}
