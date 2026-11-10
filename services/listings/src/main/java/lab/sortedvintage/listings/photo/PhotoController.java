package lab.sortedvintage.listings.photo;

import java.io.IOException;
import java.util.UUID;
import lab.sortedvintage.listings.listing.ListingView;
import lab.sortedvintage.listings.web.ApiException;
import lab.sortedvintage.listings.web.Caller;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

/** POST /listings/{id}/photos and DELETE /listings/{id}/photos/{photo_id}. */
@RestController
public class PhotoController {

    private final PhotoService service;

    public PhotoController(PhotoService service) {
        this.service = service;
    }

    @PostMapping(path = "/listings/{id}/photos", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    ResponseEntity<ListingView.PhotoView> upload(@PathVariable UUID id, @RequestPart("file") MultipartFile file,
            @RequestParam(name = "position", required = false) Integer position, Authentication auth) {
        if (file.getSize() > PhotoService.MAX_BYTES) {
            throw PhotoService.tooLarge();
        }
        byte[] bytes;
        try {
            bytes = file.getBytes();
        } catch (IOException e) {
            throw new ApiException(HttpStatus.BAD_REQUEST, "upload_failed", "The upload could not be read.");
        }
        return ResponseEntity.status(HttpStatus.CREATED).body(service.upload(id, Caller.required(auth), bytes, position));
    }

    @DeleteMapping("/listings/{id}/photos/{photoId}")
    ResponseEntity<Void> remove(@PathVariable UUID id, @PathVariable UUID photoId, Authentication auth) {
        service.remove(id, photoId, Caller.required(auth));
        return ResponseEntity.noContent().build();
    }
}
