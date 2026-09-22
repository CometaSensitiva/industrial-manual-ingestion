// Same portrait and tone mask as the CLI identity (cli_display.LOGO).
// Tones: h = hair (lavender), j = jacket (cobalt), . = line work.
export const BRAND_ASCII = "             ./\n            //      :/- ::::  .\n           ::    :///::::::::::=\n           .:.::/.   ::::::://::-::.\n         .||\\-:/  :::::::::--:::--==\\.\n          |\\|\\-:-|.-\\:::--:::-::--==\\\\\\\n     .  ..:/-----\\.::::--:-/--\\:--== .\n   ../\\///. /=-:::-\\:::::::::\\\\\\|==|\n  \\:|//-/|:/:=|.::::::::=-------=--\n   . /\\##|||--:::::::----==:------\n      |\\##\\\\\\==::.:::/|::---=-=++++_|\n         \\#\\\\**+-\\:-:\\/:=*********##|\n           \\\\###/-:::.:---**+****###\\\n             \\//:-=\\:::====*+**#**##|\\\n              //+***+=+//-+***********\\\n            _-*********|=***************\\\n            //-+******|-:+***************\\\n          :/./+*****+*\\-:+*+//_\\\\\\***#****|\n         _/ /+*******++***=|///\\\\\\|*##*##*#\n          .=*********#\\***||\\\\\\\\*|/#####*##\n         /+*******####****=\\/\\--//*########|\n.:--:::-=*******####********+=_==*#########|";
export const BRAND_TONES = "             ..\n            ..      h.. hhhh  .\n           ..    hhh.hhhhhhhhhhh\n           .....h.   hhhhhhhhhhhhhh.\n         ...hhhh  hhhhhhhhhhhhhhhhhhh.\n          .hhhhhh...hhhhhhhhhhhhhhhh...\n     .  ..hhhhh..h.hhhhhhhhhhhhhhhhh .\n   ........ hh.....hhhhhhhhhhhhhhhhh\n  ....h...hhhh........hhhhhh..hhhhh\n   . .h...hhhh..........h......hhh\n      ....hh.h........hh.......hjjjjj\n         .....jj.........jjjjjjjjjj..\n           .....j.........jjjjjjjjj..\n             ..............jjjjjjjj...\n              .jjjjjj......jjjjjjjjjjjj\n            .jjjjjjjjjj..jjjjjjjjjjjjjjjj\n            ...jjjjjjj...jjjjjjjjjjjjjjjjj\n          ...jjjjjjjjj....jjjj..jjjjjjjjjjj\n         .. jjjjjjjjjj..jjj........jjjjjjj.\n          .jjjjjjjjjjj.jjj............jjjj.\n         jjjjjjjjjjjjjjjjjj.......j....j....\n.......jjjjjjjjjj...jjjjjjjjjj...jj.........";

/** Ink density of each glyph, so small sizes can draw the portrait as a halftone. */
const DENSITY: Record<string, number> = { ".": 0.3, ":": 0.4, "-": 0.45, "_": 0.45, "=": 0.6, "+": 0.65, "/": 0.7, "\\": 0.7, "|": 0.7, "*": 0.8, "#": 1 };

/** One cell per non-space glyph: position, tone and opacity. */
export function portraitCells(): { x: number; y: number; tone: string; opacity: number }[] {
  const tones = BRAND_TONES.split("\n");
  return BRAND_ASCII.split("\n").flatMap((row, y) =>
    [...row].flatMap((glyph, x) =>
      glyph === " " ? [] : [{ x, y, tone: tones[y]?.[x] ?? ".", opacity: DENSITY[glyph] ?? 0.6 }],
    ),
  );
}
