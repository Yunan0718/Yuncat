import { useContext } from "react";
import { ToastContext } from "../components/common/toast-context";

export function useToast() {
  return useContext(ToastContext);
}
