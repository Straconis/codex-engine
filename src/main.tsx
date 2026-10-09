import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";

// A file dropped anywhere in the window would otherwise replace the app with that file.
for (const type of ["dragover", "drop"] as const) {
  window.addEventListener(type, (event) => event.preventDefault());
}

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
