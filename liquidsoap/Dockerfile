# Liquidsoap with the radio script baked in. Configure it with env vars
# (DJ_URL, ICECAST_HOST, ICECAST_SOURCE_PASSWORD, STATIONS, STREAM_BITRATE).
FROM savonet/liquidsoap:v2.3.3
COPY radio.liq /liquidsoap/radio.liq
CMD ["/liquidsoap/radio.liq"]
