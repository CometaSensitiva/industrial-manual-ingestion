// Same portrait and tone mask as the CLI identity (cli_display.LOGO).
// Tones: h = hair (lavender), j = jacket (cobalt), . = line work.
export const BRAND_ASCII = "             ./\n            //      :/- ::::  .\n           ::    :///::::::::::=\n           .:.::/.   ::::::://::-::.\n         .||\\-:/  :::::::::--:::--==\\.\n          |\\|\\-:-|.-\\:::--:::-::--==\\\\\\\n     .  ..:/-----\\.::::--:-/--\\:--== .\n   ../\\///. /=-:::-\\:::::::::\\\\\\|==|\n  \\:|//-/|:/:=|.::::::::=-------=--\n   . /\\##|||--:::::::----==:------\n      |\\##\\\\\\==::.:::/|::---=-=++++_|\n         \\#\\\\**+-\\:-:\\/:=*********##|\n           \\\\###/-:::.:---**+****###\\\n             \\//:-=\\:::====*+**#**##|\\\n              //+***+=+//-+***********\\\n            _-*********|=***************\\\n            //-+******|-:+***************\\\n          :/./+*****+*\\-:+*+//_\\\\\\***#****|\n         _/ /+*******++***=|///\\\\\\|*##*##*#\n          .=*********#\\***||\\\\\\\\*|/#####*##\n         /+*******####****=\\/\\--//*########|\n.:--:::-=*******####********+=_==*#########|";
export const BRAND_TONES = "             ..\n            ..      h.. hhhh  .\n           ..    hhh.hhhhhhhhhhh\n           .....h.   hhhhhhhhhhhhhh.\n         ...hhhh  hhhhhhhhhhhhhhhhhhh.\n          .hhhhhh...hhhhhhhhhhhhhhhh...\n     .  ..hhhhh..h.hhhhhhhhhhhhhhhhh .\n   ........ hh.....hhhhhhhhhhhhhhhhh\n  ....h...hhhh........hhhhhh..hhhhh\n   . .h...hhhh..........h......hhh\n      ....hh.h........hh.......hjjjjj\n         .....jj.........jjjjjjjjjj..\n           .....j.........jjjjjjjjj..\n             ..............jjjjjjjj...\n              .jjjjjj......jjjjjjjjjjjj\n            .jjjjjjjjjj..jjjjjjjjjjjjjjjj\n            ...jjjjjjj...jjjjjjjjjjjjjjjjj\n          ...jjjjjjjjj....jjjj..jjjjjjjjjjj\n         .. jjjjjjjjjj..jjj........jjjjjjj.\n          .jjjjjjjjjjj.jjj............jjjj.\n         jjjjjjjjjjjjjjjjjj.......j....j....\n.......jjjjjjjjjj...jjjjjjjjjj...jj.........";

/** Split each portrait row into runs of equal tone, ready to colour. */
export function portraitRuns(): { text: string; tone: string }[][] {
  const tones = BRAND_TONES.split("\n");
  return BRAND_ASCII.split("\n").map((row, y) => {
    const runs: { text: string; tone: string }[] = [];
    for (let x = 0; x < row.length; x++) {
      const tone = tones[y]?.[x] ?? ".";
      const last = runs.at(-1);
      if (last && last.tone === tone) last.text += row[x];
      else runs.push({ text: row[x]!, tone });
    }
    return runs;
  });
}
