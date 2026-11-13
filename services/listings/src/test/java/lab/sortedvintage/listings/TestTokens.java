package lab.sortedvintage.listings;

import com.nimbusds.jose.JWSAlgorithm;
import com.nimbusds.jose.JWSHeader;
import com.nimbusds.jose.crypto.RSASSASigner;
import com.nimbusds.jose.jwk.JWKSet;
import com.nimbusds.jose.jwk.KeyUse;
import com.nimbusds.jose.jwk.RSAKey;
import com.nimbusds.jose.jwk.gen.RSAKeyGenerator;
import com.nimbusds.jwt.JWTClaimsSet;
import com.nimbusds.jwt.SignedJWT;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Date;
import java.util.List;
import java.util.UUID;

/** A generated signing key (nothing committed) and a local JWKS endpoint standing in for Keycloak. */
public final class TestTokens {

    public static final String ISSUER = "https://auth.invalid:8443/realms/sv-test";

    private final RSAKey key;
    private final HttpServer jwks;
    private final java.util.concurrent.atomic.AtomicInteger tokenRequests = new java.util.concurrent.atomic.AtomicInteger();

    public TestTokens() {
        try {
            key = new RSAKeyGenerator(2048).keyUse(KeyUse.SIGNATURE).keyID("test-key").generate();
            byte[] body = new JWKSet(key.toPublicJWK()).toString().getBytes(StandardCharsets.UTF_8);
            jwks = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            jwks.createContext("/certs", exchange -> {
                exchange.getResponseHeaders().add("Content-Type", "application/json");
                exchange.sendResponseHeaders(200, body.length);
                exchange.getResponseBody().write(body);
                exchange.close();
            });
            // Keycloak's token endpoint for Listings' client-credentials token (T046).
            jwks.createContext("/token", exchange -> {
                tokenRequests.incrementAndGet();
                String form = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
                String auth = exchange.getRequestHeaders().getFirst("Authorization");
                boolean ok = form.contains("grant_type=client_credentials") && auth != null && auth.startsWith("Basic ");
                byte[] reply = (ok
                        ? "{\"access_token\":\"" + token(ISSUER, List.of("payments"), Instant.now().plusSeconds(300),
                                UUID.randomUUID(), null) + "\",\"expires_in\":300,\"token_type\":\"Bearer\"}"
                        : "{\"error\":\"invalid_client\"}").getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().add("Content-Type", "application/json");
                exchange.sendResponseHeaders(ok ? 200 : 401, reply.length);
                exchange.getResponseBody().write(reply);
                exchange.close();
            });
            jwks.start();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /** Client-credentials requests answered so far; Listings should cache its token. */
    public int tokenRequests() {
        return tokenRequests.get();
    }

    public String tokenUri() {
        return "http://127.0.0.1:" + jwks.getAddress().getPort() + "/token";
    }

    public String jwksUri() {
        return "http://127.0.0.1:" + jwks.getAddress().getPort() + "/certs";
    }

    public String token(String issuer, List<String> audience, Instant expires) {
        return token(issuer, audience, expires, UUID.randomUUID(), null);
    }

    /** A seller's token as Keycloak issues it: subject = account_sub, plus the display_name claim. */
    public String seller(UUID accountSub, String displayName) {
        return token(ISSUER, List.of("listings", "orders", "payments"), Instant.now().plusSeconds(300),
                accountSub, displayName);
    }

    /** Another service's client-credentials token, e.g. Orders' (azp orders, listings in aud). */
    public String service(String azp) {
        return token(ISSUER, List.of("listings", "payments"), Instant.now().plusSeconds(300),
                UUID.randomUUID(), null, azp);
    }

    private String token(String issuer, List<String> audience, Instant expires, UUID subject, String displayName) {
        return token(issuer, audience, expires, subject, displayName, "bff");
    }

    private String token(String issuer, List<String> audience, Instant expires, UUID subject, String displayName,
            String azp) {
        try {
            JWTClaimsSet.Builder builder = new JWTClaimsSet.Builder()
                    .issuer(issuer)
                    .subject(subject.toString())
                    .audience(audience)
                    .issueTime(Date.from(Instant.now().minusSeconds(60)))
                    .expirationTime(Date.from(expires))
                    .claim("typ", "Bearer")
                    .claim("azp", azp);
            if (displayName != null) {
                builder.claim("display_name", displayName);
            }
            JWTClaimsSet claims = builder.build();
            SignedJWT jwt = new SignedJWT(
                    new JWSHeader.Builder(JWSAlgorithm.RS256).keyID(key.getKeyID()).build(), claims);
            jwt.sign(new RSASSASigner(key));
            return jwt.serialize();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    public String valid() {
        return token(ISSUER, List.of("listings"), Instant.now().plusSeconds(300));
    }
}
