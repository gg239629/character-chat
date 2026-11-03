// One 24px outline vocabulary for chat controls. No network or font dependency.
export const icons={
 send:['M4 4 21 12 4 20l3-8-3-8Z','M7 12h14'],
 image:['M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z','m3 16 6-6 5 5 3-3 4 4','M16 8h.01'],
 wand:['m4 17 11-11 4 4L8 21Z','m12 9 4 4','M5 2v4M3 4h4M20 16v5M17.5 18.5h5'],
 close:['m6 6 12 12','M18 6 6 18'],
 settings:['M4 6h8m4 0h4M4 12h3m4 0h9M4 18h10m4 0h2','M12 3v6M7 9v6M14 15v6'],
 back:['m15 5-7 7 7 7'],
 next:['m9 5 7 7-7 7'],
 refresh:['M20 10a8 8 0 1 0-1 8','M20 4v6h-6'],
 download:['M12 3v12m-5-5 5 5 5-5','M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4'],
 save:['M5 3h12l4 4v13a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a2 2 0 0 1 2-2Z','M7 3v6h9V3M7 21v-7h10v7'],
 connect:['M8 3v5m8-5v5M6 8h12v3a6 6 0 0 1-12 0V8ZM12 17v4'],
 sparkles:['m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z','M20 2v4m-2-2h4'],
 trash:['M3 6h18M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7'],
 logout:['M10 4H5v16h5M9 12h12m-4-4 4 4-4 4'],
 key:['M14 7a5 5 0 1 1-4-4 5 5 0 0 1 4 4ZM13 11l8 8m-4-4-3 3m5-1-3 3'],
 check:['m5 12 4 4L19 6'],
 bell:['M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4'],
 bellOff:['M18 12V8a6 6 0 0 0-9-5M6 6c-1 2 0 8-3 11h14M10 21h4M3 3l18 18'],
 user:['M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v2'],
 loader:['M20 12a8 8 0 1 1-8-8'],
} as const;
export type IconName=keyof typeof icons;
