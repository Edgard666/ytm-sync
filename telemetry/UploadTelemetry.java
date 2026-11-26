package org.araymond.joal.telemetry;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.regex.Pattern;
import org.araymond.joal.core.events.announce.FailedToAnnounceEvent;
import org.araymond.joal.core.events.announce.SuccessfullyAnnounceEvent;
import org.araymond.joal.core.events.announce.TooManyAnnouncesFailedEvent;
import org.araymond.joal.core.ttorrent.client.announcer.AnnouncerFacade;
import org.springframework.context.event.EventListener;
import org.araymond.joal.core.SeedManager;
import org.araymond.joal.core.bandwith.BandwidthDispatcher;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.scheduling.annotation.Scheduled;

@Configuration
@EnableScheduling
public class UploadTelemetry {
    private final SeedManager manager;
    private final ObjectMapper json = new ObjectMapper();
    private final Field dispatcherField;
    // Last announce outcome per infohash: {ok (0/1), epoch seconds, sanitized message}.
    private final Map<String, Map<String, Object[]>> announces = new ConcurrentHashMap<>();
    // Tracker URLs may embed passkeys: never let them reach the controller.
    private static final Pattern URL = Pattern.compile("[a-zA-Z][a-zA-Z0-9+.-]*://\\S*");

    // Retain only tokens consumed by announce_category(), never free-form tracker text.
    private static final Pattern REASON = Pattern.compile(
        "too many announces failed|passkey|auth|credential|unregistered|unknown torrent|torrent not found|"
        + "not registered|not exist|client|peer id|peer_id|banned|too frequent|too soon|rate limit|slow down|"
        + "no response|timed out|timeout|connection|connect|refused|unknownhost|no more valid tracker|"
        + "protocol|reading tracker|read tracker|status [0-9]{3}");

    public UploadTelemetry(SeedManager manager) throws ReflectiveOperationException {
        this.manager = manager;
        dispatcherField = SeedManager.class.getDeclaredField("bandwidthDispatcher");
        dispatcherField.setAccessible(true);
    }

    private static String hex(AnnouncerFacade announcer) {
        return HexFormat.of().formatHex(announcer.getTorrentInfoHash().value().getBytes(StandardCharsets.ISO_8859_1));
    }

    private static final Map<Object, String> lastHosts = Collections.synchronizedMap(new WeakHashMap<>());

    public static void recordHost(java.net.URI uri, Object provider) {
        if (uri != null && uri.getHost() != null) lastHosts.put(provider, uri.getHost().toLowerCase(Locale.ROOT));
    }

    private static Object fieldOfType(Object owner, String type) throws ReflectiveOperationException {
        for (Field field : owner.getClass().getDeclaredFields()) {
            if (field.getType().getSimpleName().equals(type)) {
                field.setAccessible(true);
                return field.get(owner);
            }
        }
        throw new NoSuchFieldException(type);
    }

    private static String takeHost(AnnouncerFacade announcer) {
        try {
            Object client = fieldOfType(announcer, "TrackerClient");
            Object provider = fieldOfType(client, "TrackerClientUriProvider");
            return Optional.ofNullable(lastHosts.remove(provider)).orElse("");
        } catch (ReflectiveOperationException | RuntimeException ignored) {
            return "";
        }
    }

    private void remember(AnnouncerFacade announcer, boolean ok, String message) {
        String safe = message == null ? "" : URL.matcher(message).replaceAll("<tracker>");
        safe = REASON.matcher(safe.toLowerCase(Locale.ROOT)).results()
            .map(match -> match.group()).distinct().collect(java.util.stream.Collectors.joining(" "));
        announces.computeIfAbsent(hex(announcer), key -> new ConcurrentHashMap<>())
            .put(takeHost(announcer), new Object[]{ok ? 1 : 0, System.currentTimeMillis() / 1000, safe});
    }

    @EventListener
    public void onSuccess(SuccessfullyAnnounceEvent event) {
        remember(event.getAnnouncerFacade(), true, null);
    }

    @EventListener
    public void onFailure(FailedToAnnounceEvent event) {
        remember(event.getAnnouncerFacade(), false, event.getErrMessage());
    }

    @EventListener
    public void onTooManyFailures(TooManyAnnouncesFailedEvent event) {
        // A local stop after repeated failures is not another tracker response.
        if (!announces.containsKey(hex(event.getAnnouncerFacade())))
            remember(event.getAnnouncerFacade(), false, "too many announces failed in a row");
    }

    @Scheduled(fixedDelay = 1000)
    public void sample() throws Exception {
        String location = System.getProperty("controller.telemetry");
        if (location == null) return;
        BandwidthDispatcher dispatcher = (BandwidthDispatcher) dispatcherField.get(manager);
        if (dispatcher == null) return;
        Map<String, long[]> rows = new HashMap<>();
        dispatcher.getSpeedMap().forEach((hash, speed) -> {
            String hex = HexFormat.of().formatHex(hash.value().getBytes(StandardCharsets.ISO_8859_1));
            rows.put(hex, new long[]{speed.getBytesPerSecond(), dispatcher.getSeedStatForTorrent(hash).getUploaded()});
        });
        manager.getCurrentlySeedingAnnouncers().forEach(announcer -> {
            String hex = HexFormat.of().formatHex(announcer.getTorrentInfoHash().value().getBytes(StandardCharsets.ISO_8859_1));
            // At 0 kB/s the dispatcher may not list the torrent: still publish
            // the tracker counters with a zero rate and volume.
            long[] upload = rows.getOrDefault(hex, new long[]{0L, 0L});
            rows.put(hex, new long[]{upload[0], upload[1],
                announcer.getLastKnownSeeders().orElse(-1), announcer.getLastKnownLeechers().orElse(-1),
                announcer.getLastAnnouncedAt().map(t -> t.atZone(java.time.ZoneId.systemDefault()).toEpochSecond()).orElse(0L)});
        });
        Path dest = Path.of(location);
        Path staging = dest.resolveSibling("upload.tmp");
        json.writeValue(staging.toFile(), rows);
        Files.move(staging, dest, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
        Path announceDest = dest.resolveSibling("announce.json");
        Path announceStaging = dest.resolveSibling("announce.tmp");
        json.writeValue(announceStaging.toFile(), announces);
        Files.move(announceStaging, announceDest, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }
}
