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
            jwks.start();
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    public String jwksUri() {
        return "http://127.0.0.1:" + jwks.getAddress().getPort() + "/certs";
    }

    public String token(String issuer, List<String> audience, Instant expires) {
        try {
            JWTClaimsSet claims = new JWTClaimsSet.Builder()
                    .issuer(issuer)
                    .subject(UUID.randomUUID().toString())
                    .audience(audience)
                    .issueTime(Date.from(Instant.now().minusSeconds(60)))
                    .expirationTime(Date.from(expires))
                    .claim("typ", "Bearer")
                    .build();
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
