import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { translateSaved as t } from "../../../../i18n/runtime";
import { Box, Typography } from "../../../../theme/ui";
import { buildLyricLines, lyricLineIndex, lyricSyllableFill, lyricWordFill } from "./timeline";

const fillSx = {
  position: "absolute",
  inset: 0,
  color: "var(--color-primary-hover)",
  WebkitTextFillColor: "var(--color-primary-hover)",
  clipPath: "inset(0 calc(100% - var(--character-fill)) 0 0)",
  filter: "drop-shadow(0 0 .18em color-mix(in srgb, var(--color-primary) 8%, transparent))",
  pointerEvents: "none",
  willChange: "clip-path"
};

const Fill = ({ children, dataRole }) => (
  <Box as="span" aria-hidden data-role={dataRole} sx={fillSx}>
    {children}
  </Box>
);

const lineStyle = (distance) => ({
  opacity: distance === 0 ? 1 : distance === 1 ? 0.5 : 0.18,
  transform: `translateY(${distance * 5.4}rem) translateY(-50%) scale(${
    distance === 0 ? 1 : distance === 1 ? 0.88 : 0.78
  })`,
  filter: distance === 0 ? "none" : `blur(${distance === 1 ? 0.3 : 0.8}px)`,
  zIndex: 4 - distance
});

function KaraokeLyrics({ lyricsSync, currentTime = 0, currentTimeRef, isPlaying = false }) {
  const lines = useMemo(() => buildLyricLines(lyricsSync), [lyricsSync]);
  const time = Number(currentTimeRef?.current ?? currentTime) || 0;
  const [currentLine, setCurrentLine] = useState(() => lyricLineIndex(lines, time));
  const refs = useRef(new Map());

  const bind = (key, item, fill) => (node) => {
    if (node) refs.current.set(key, [node, item, fill]);
    else refs.current.delete(key);
  };

  const update = useCallback(
    (time) => {
      const next = lyricLineIndex(lines, time);

      setCurrentLine((line) => (line === next ? line : next));

      refs.current.forEach(([node, item, fill]) => {
        node.style.setProperty("--character-fill", fill(item, time));
      });
    },
    [lines]
  );

  useLayoutEffect(() => update(time), [time, update]);

  useEffect(() => {
    if (!isPlaying || !currentTimeRef || !globalThis.requestAnimationFrame) return;

    let frame;

    const render = () => {
      update(Number(currentTimeRef.current) || 0);
      frame = requestAnimationFrame(render);
    };

    frame = requestAnimationFrame(render);
    return () => cancelAnimationFrame(frame);
  }, [currentTimeRef, isPlaying, update]);

  if (!lines.length) {
    return <Typography tone="muted">{t("common.syncedTextNotAvailable")}</Typography>;
  }

  const visibleLines = lines.slice(currentLine, currentLine + 4);

  return (
    <Box
      data-role="lyrics"
      sx={{
        position: "absolute",
        inset: "50% auto auto 50%",
        transform: "translate(-50%, -50%)",
        inlineSize: "min(88rem, 92vw)",
        blockSize: "min(10rem, 40vh)",
        padding: "var(--space-4) var(--space-8)",
        border: "1px solid color-mix(in srgb, var(--color-text) 10%, transparent)",
        borderRadius: "var(--shape-xl)",
        background:
          "linear-gradient(180deg, color-mix(in srgb, var(--color-bg-deep) 26%, transparent), color-mix(in srgb, var(--color-bg-deep) 12%, transparent))",
        boxShadow:
          "0 var(--space-4) var(--space-10) color-mix(in srgb, var(--color-bg-deep) 26%, transparent), inset 0 1px 0 color-mix(in srgb, var(--color-text) 9%, transparent)",
        backdropFilter: "blur(var(--space-4)) saturate(1.18)",
        WebkitMaskImage:
          "linear-gradient(to bottom, transparent 0%, #000 14%, #000 86%, transparent 100%), linear-gradient(to right, transparent 0%, #000 7%, #000 93%, transparent 100%)",
        WebkitMaskComposite: "source-in",
        maskImage:
          "linear-gradient(to bottom, transparent 0%, #000 14%, #000 86%, transparent 100%), linear-gradient(to right, transparent 0%, #000 7%, #000 93%, transparent 100%)",
        maskComposite: "intersect",
        zIndex: 4,
        pointerEvents: "none",
        overflow: "hidden"
      }}
    >
      {visibleLines.map((words, distance) => {
        const line = currentLine + distance;
        const current = distance === 0;

        return (
          <Typography
            key={line}
            as="p"
            data-role="lyric-line"
            data-current={current || undefined}
            variant="h2"
            align="center"
            sx={{
              zIndex: 99999,
              position: "absolute",
              insetBlockStart: "25%",
              insetInlineStart: "50%",
              inlineSize: "calc(100% - var(--space-16))",
              margin: 0,
              textAlign: "center",
              whiteSpace: "normal",
              textWrap: "balance",
              overflow: "visible",
              color: current ? "var(--color-text)" : "var(--color-text-muted)",
              WebkitTextFillColor: current ? "var(--color-text)" : "var(--color-text-muted)",
              fontWeight: current ? 850 : 650,
              letterSpacing: current ? "-0.024em" : "-0.018em",
              lineHeight: 1.05,
              textShadow: current
                ? "0 .08em .12em color-mix(in srgb, var(--color-bg-deep) 82%, transparent), 0 .25em .8em color-mix(in srgb, var(--color-bg-deep) 48%, transparent), 0 0 .8em color-mix(in srgb, var(--color-primary) 16%, transparent)"
                : "0 .12em .35em color-mix(in srgb, var(--color-bg-deep) 62%, transparent)",
              transition:
                "transform var(--motion-duration-slow) var(--motion-easing-spring), opacity var(--motion-duration-normal) var(--motion-easing-standard), filter var(--motion-duration-normal) var(--motion-easing-standard)",
              transformOrigin: "center",
              willChange: "transform, opacity, filter",
              translate: "-50% 0",
              ...lineStyle(distance)
            }}
          >
            {words.map((word, wordIndex) => {
              const syllables =
                word.syllables?.length > 1 &&
                word.syllables.map(({ text }) => text).join("") === word.text
                  ? word.syllables
                  : null;

              return (
                <Box
                  as="span"
                  key={`${word.index}-${wordIndex}`}
                  ref={syllables ? undefined : bind(`${line}:${wordIndex}`, word, lyricWordFill)}
                  data-role="lyric-word"
                  data-text={word.text}
                  data-start={word.start}
                  data-end={word.end}
                  style={syllables ? undefined : { "--character-fill": lyricWordFill(word, time) }}
                  sx={{
                    position: "relative",
                    display: "inline-block",
                    marginInlineEnd: "var(--space-2)",
                    color: current ? "var(--color-text)" : "var(--color-text-muted)",
                    WebkitTextFillColor: current ? "var(--color-text)" : "var(--color-text-muted)"
                  }}
                >
                  {syllables ? (
                    syllables.map((syllable, syllableIndex) => (
                      <Box
                        as="span"
                        key={`${syllable.start}-${syllableIndex}`}
                        ref={bind(
                          `${line}:${wordIndex}:${syllableIndex}`,
                          syllable,
                          lyricSyllableFill
                        )}
                        data-role="lyric-syllable"
                        data-text={syllable.text}
                        data-start={syllable.start}
                        data-end={syllable.end}
                        style={{ "--character-fill": lyricSyllableFill(syllable, time) }}
                        sx={{ position: "relative", display: "inline-block" }}
                      >
                        {syllable.text}
                        <Fill dataRole="lyric-syllable-fill">{syllable.text}</Fill>
                      </Box>
                    ))
                  ) : (
                    <>
                      {word.text}
                      <Fill dataRole="lyric-word-fill">{word.text}</Fill>
                    </>
                  )}
                </Box>
              );
            })}
          </Typography>
        );
      })}
    </Box>
  );
}

export default memo(
  KaraokeLyrics,
  (prev, next) =>
    prev.lyricsSync === next.lyricsSync &&
    prev.currentTimeRef === next.currentTimeRef &&
    prev.isPlaying === next.isPlaying &&
    (!!(next.isPlaying && next.currentTimeRef) || prev.currentTime === next.currentTime)
);
