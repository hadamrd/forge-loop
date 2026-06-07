import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "@tanstack/react-router";
import { ApiProvider } from "@/api/ApiProvider";
import { UiProvider } from "@/lib/ui";
import { router } from "@/router";
import "@/index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ApiProvider>
      <UiProvider>
        <RouterProvider router={router} />
      </UiProvider>
    </ApiProvider>
  </StrictMode>,
);
