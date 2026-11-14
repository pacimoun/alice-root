/* Verity SoundCloud voice extension.
 * Only explicitly prefixed music requests may leave the station.
 * The existing agent retains its request-id, final/BUSY and duplicate guards.
 */
static int verity_music_prefix(const char *value) {
    static const char *markers[] = {"музыка "};
    size_t length = strlen(value);
    for (size_t i = 0; i < sizeof(markers) / sizeof(markers[0]); ++i) {
        size_t n = strlen(markers[i]);
        if (length > n && !memcmp(value, markers[i], n)) return 1;
    }
    return 0;
}

static void verity_music_event(struct agent *a, const unsigned char *value, size_t length) {
    /* Worst-case JSON escaping is six bytes per input byte. Never interpolate
     * an ASR string into a format string, shell command or unescaped JSON. */
    char text[6400];
    const char prefix[] = "{\"event\":\"verity_music\",\"text\":\"";
    if (length > 1024) return;
    size_t n = sizeof(prefix) - 1;
    memcpy(text, prefix, n);
    const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < length; ++i) {
        unsigned int c = value[i];
        if (c == '"' || c == '\\') {
            text[n++] = '\\'; text[n++] = (char)c;
        } else if (c < 32) {
            text[n++] = '\\'; text[n++] = 'u'; text[n++] = '0'; text[n++] = '0';
            text[n++] = hex[c >> 4]; text[n++] = hex[c & 15];
        } else text[n++] = (char)c;
    }
    text[n++] = '"'; text[n++] = '}'; text[n++] = '\n';
    if (!output(1, text, n)) stopping = 1;
    a->commands++;
}
