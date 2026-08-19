import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./globals.css";
import VerifyPage from "./verify/page";

const root = document.getElementById("root");

if (!root) throw new Error("Application root was not found");

createRoot(root).render(
  <StrictMode>
    <VerifyPage />
  </StrictMode>,
);
