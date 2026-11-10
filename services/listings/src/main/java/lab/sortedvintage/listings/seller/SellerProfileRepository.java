package lab.sortedvintage.listings.seller;

import java.util.UUID;
import lab.sortedvintage.listings.web.Caller;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/** Seller profile rows (data-model.md): created on the first draft, display name refreshed on every write. */
@Repository
public class SellerProfileRepository {

    private final JdbcClient jdbc;

    public SellerProfileRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    /** Creates the profile if missing and copies the token's display name (FR-004, data-model.md). */
    public void upsert(Caller caller) {
        jdbc.sql("""
                insert into seller_profile (account_sub, display_name) values (:sub, :name)
                on conflict (account_sub) do update set display_name = excluded.display_name
                where seller_profile.display_name is distinct from excluded.display_name
                """)
                .param("sub", caller.sub())
                .param("name", caller.displayName())
                .update();
    }
}
