// Icon — thin wrapper over lucide-react so the whole app references icons by the
// PascalCase names declared once in lib/theme.ts (EVENT_META, SEVERITY_META, …).
// Never import lucide icons directly in a component; always go through <Icon name="…" />.
import { icons, type LucideProps } from "lucide-react";
import type { CSSProperties } from "react";

export interface IconProps {
  name: string;
  size?: number;
  color?: string;
  strokeWidth?: number;
  className?: string;
  spin?: boolean;
  style?: CSSProperties;
}

export function Icon({
  name,
  size = 16,
  color = "currentColor",
  strokeWidth = 2,
  className,
  spin,
  style,
}: IconProps) {
  const Cmp = (icons as Record<string, React.ComponentType<LucideProps>>)[name] ?? icons.Circle;
  return (
    <Cmp
      size={size}
      color={color}
      strokeWidth={strokeWidth}
      className={(className ?? "") + (spin ? " spin" : "")}
      style={style}
      aria-hidden
    />
  );
}
