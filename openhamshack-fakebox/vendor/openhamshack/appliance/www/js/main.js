  // Last of the console's scripts: refresh() paints every area, so the
  // polling starts only once all of them have loaded. (When this was the
  // end of the first of six inline scripts, refresh's first await was
  // what let the other five load before it painted.)

  refresh();
  setInterval(refresh, 1000);
