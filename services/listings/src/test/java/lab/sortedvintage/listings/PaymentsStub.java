package lab.sortedvintage.listings;

import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * Stands in for Payments' {@code GET /connected-accounts/{account_sub}/payout-status}
 * (contracts/payments.openapi.yaml), which Listings asks before publishing. Every seller is
 * {@code ready} unless a test says otherwise. It records the Authorization header of each call.
 */
final class PaymentsStub {

    private static final String PREFIX = "/connected-accounts/";
    private static final String SUFFIX = "/payout-status";

    private final Map<UUID, String> statuses = new ConcurrentHashMap<>();
    private final List<String> authorizations = new CopyOnWriteArrayList<>();
    private final HttpServer server;

    PaymentsStub() {
        try {
            server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            server.createContext(PREFIX, exchange -> {
                String path = exchange.getRequestURI().getPath();
                String auth = exchange.getRequestHeaders().getFirst("Authorization");
                authorizations.add(auth == null ? "" : auth);
                byte[] body;
                int code;
                if (!"GET".equals(exchange.getRequestMethod()) || !path.endsWith(SUFFIX)) {
                    code = 404;
                    body = "{\"error\":\"not_found\",\"message\":\"No such route.\"}".getBytes(StandardCharsets.UTF_8);
                } else {
                    UUID sub = UUID.fromString(path.substring(PREFIX.length(), path.length() - SUFFIX.length()));
                    code = 200;
                    body = ("{\"payout_status\":\"" + statuses.getOrDefault(sub, "ready") + "\"}")
                            .getBytes(StandardCharsets.UTF_8);
                }
                exchange.getResponseHeaders().add("Content-Type", "application/json");
                exchange.sendResponseHeaders(code, body.length);
                exchange.getResponseBody().write(body);
                exchange.close();
            });
            server.start();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    String url() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    void payoutStatus(UUID sub, String status) {
        statuses.put(sub, status);
    }

    List<String> authorizations() {
        return authorizations;
    }
}
