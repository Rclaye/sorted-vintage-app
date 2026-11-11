package lab.sortedvintage.listings.config;

import java.io.IOException;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.web.AuthenticationEntryPoint;
import org.springframework.security.web.SecurityFilterChain;

/**
 * Resource server for Keycloak access tokens. The decoder is built by Spring Boot from
 * spring.security.oauth2.resourceserver.jwt.*: keys come from jwk-set-uri, and issuer-uri is only
 * compared with the iss claim (no OIDC discovery, because pods cannot reach the browser address).
 * The audiences property adds the aud check.
 */
@Configuration
public class SecurityConfig {

    @Bean
    SecurityFilterChain api(HttpSecurity http) throws Exception {
        http.csrf(AbstractHttpConfigurer::disable)
                .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(a -> a
                        .requestMatchers("/healthz", "/metrics").permitAll()
                        // Spring's error page: without this a 404 on a public route turns into a 401.
                        .requestMatchers("/error").permitAll()
                        // Public reads (FR-007, FR-008). Writes and reservations need a token.
                        .requestMatchers(HttpMethod.GET, "/listings", "/listings/*", "/sellers/*").permitAll()
                        // Photo bytes: public for published listings, owner-only for drafts (checked there).
                        .requestMatchers(HttpMethod.GET, "/photos/**").permitAll()
                        .anyRequest().authenticated())
                .oauth2ResourceServer(o -> o
                        .jwt(Customizer.withDefaults())
                        .authenticationEntryPoint(entryPoint()));
        return http.build();
    }

    /** 401 with the contract's error body and a Bearer challenge. */
    private static AuthenticationEntryPoint entryPoint() {
        return (request, response, ex) -> writeError(response, 401, "invalid_token",
                request.getHeader("Authorization") == null ? "A bearer token is required." : "The token is not valid.");
    }

    static void writeError(jakarta.servlet.http.HttpServletResponse response, int status, String code, String message)
            throws IOException {
        response.setStatus(status);
        if (status == 401) {
            response.setHeader("WWW-Authenticate", "Bearer");
        }
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.getWriter().write("{\"error\":\"" + code + "\",\"message\":\"" + message + "\"}");
    }
}
