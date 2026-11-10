package lab.sortedvintage.listings.photo;

import java.util.Arrays;
import java.util.Optional;

/**
 * The accepted photo types (FR-005), recognised from the file's first bytes, never from its name
 * or the type the browser declared.
 */
enum ImageType {

    JPEG("image/jpeg"),
    PNG("image/png"),
    WEBP("image/webp");

    private static final byte[] JPEG_MAGIC = {(byte) 0xFF, (byte) 0xD8, (byte) 0xFF};
    private static final byte[] PNG_MAGIC = {(byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n'};
    private static final byte[] RIFF = {'R', 'I', 'F', 'F'};
    private static final byte[] WEBP_MAGIC = {'W', 'E', 'B', 'P'};

    final String contentType;

    ImageType(String contentType) {
        this.contentType = contentType;
    }

    static Optional<ImageType> detect(byte[] bytes) {
        if (startsWith(bytes, 0, JPEG_MAGIC)) {
            return Optional.of(JPEG);
        }
        if (startsWith(bytes, 0, PNG_MAGIC)) {
            return Optional.of(PNG);
        }
        if (startsWith(bytes, 0, RIFF) && startsWith(bytes, 8, WEBP_MAGIC)) {
            return Optional.of(WEBP);
        }
        return Optional.empty();
    }

    private static boolean startsWith(byte[] bytes, int offset, byte[] magic) {
        return bytes.length >= offset + magic.length
                && Arrays.equals(bytes, offset, offset + magic.length, magic, 0, magic.length);
    }
}
