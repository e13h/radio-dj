# Liquidsoap mixer for the AI radio stations.
#
# Liquidsoap is deliberately dumb here: for every station it asks the DJ
# service "what's next?", plays whatever file comes back, and tells the DJ
# service when each item actually goes on air. All programming decisions
# (track selection, DJ breaks, deep dives) live in the radio-dj service.

settings.log.stdout.set(true)
settings.log.file.set(false)
settings.log.level.set(3)

dj_url = environment.get(default="http://radio-dj:8080", "DJ_URL")
ice_host = environment.get(default="radio-icecast", "ICECAST_HOST")
ice_password = environment.get("ICECAST_SOURCE_PASSWORD")
bitrate = int_of_string(default=192, environment.get(default="192", "STREAM_BITRATE"))
stations = string.split(separator=",", environment.get(default="main", "STATIONS"))

# Level everything to the same ReplayGain reference: songs from beets carry
# ReplayGain tags; anything without them (DJ voice clips, untagged songs) is
# measured when it's prepared. A DJ's voice can be nudged further with the
# liq_amplify metadata (DJ `gain:` in stations.yaml, e.g. "+2 dB").
enable_replaygain_metadata(compute=true)

def make_station(station_id) =
  # Ask the DJ service for the next item. It blocks until the item is fully
  # prepared (track downloaded / voice rendered), hence the long timeout.
  def next_item() =
    uri =
      try
        resp = http.get(timeout=300., "#{dj_url}/api/stations/#{station_id}/next")
        if resp.status_code == 200 then string.trim(resp) else "" end
      catch err do
        log.important(label=station_id, "DJ service unreachable: #{err.message}")
        ""
      end
    if uri == "" then
      null()
    else
      request.create(uri)
    end
  end

  queue = request.dynamic(id="#{station_id}_queue", prefetch=1, retry_delay=5., next_item)

  # Tell the DJ service what actually went on air (for history, scrobbles and
  # the now-playing page). Run asynchronously: on_track handlers must be fast.
  def on_air(m) =
    item_id = m["radio_item"]
    if item_id != "" then
      thread.run(
        fast=false,
        fun () ->
          try
            ignore(
              http.post(
                timeout=10.,
                data="",
                "#{dj_url}/api/stations/#{station_id}/aired?item=#{url.encode(item_id)}"
              )
            )
          catch err do
            log.important(label=station_id, "Failed to report aired item: #{err.message}")
          end
      )
    end
  end
  queue = source.on_track(queue, on_air)

  # Music crossfades into music. Around a DJ break, the voice item's
  # radio_overlap metadata (talk.overlap_seconds in stations.yaml) says how
  # many seconds the voice and music overlap: 0 plays them back to back, so
  # the DJ never talks over a song. a.source is the last `crossfade` seconds
  # of the outgoing item; b starts `overlap` seconds before it ends.
  crossfade = 3.
  def transition(a, b) =
    if a.metadata["radio_kind"] == "voice" or b.metadata["radio_kind"] == "voice" then
      voice = if a.metadata["radio_kind"] == "voice" then a.metadata else b.metadata end
      overlap = min(crossfade, max(0., float_of_string(default=0., voice["radio_overlap"])))
      if overlap == 0. then
        sequence([a.source, b.source])
      else
        add(normalize=false, [a.source, sequence([blank(duration=crossfade - overlap), b.source])])
      end
    else
      cross.simple(fade_out=crossfade, fade_in=1.5, a.source, b.source)
    end
  end

  s = replaygain(queue)
  s = amplify(1., s)
  s = cross(duration=crossfade, transition, s)
  s = mksafe(s)

  output.icecast(
    %mp3(bitrate=bitrate, samplerate=44100, stereo=true),
    id="#{station_id}_icecast",
    host=ice_host,
    port=8000,
    password=ice_password,
    mount="#{station_id}.mp3",
    name=station_id,
    s
  )
end

list.iter(make_station, stations)
