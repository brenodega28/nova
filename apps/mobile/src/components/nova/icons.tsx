import Svg, { Line, Path, Polyline, Rect } from 'react-native-svg';

import type { ConfigIcon } from '@/constants/assistant';

type IconProps = {
  color: string;
  size?: number;
};

export function MicrophoneIcon({ color, size = 19 }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Rect
        x={9}
        y={2}
        width={6}
        height={12}
        rx={3}
        stroke={color}
        strokeWidth={1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <Path
        d="M5 11a7 7 0 0 0 14 0"
        stroke={color}
        strokeWidth={1.7}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <Line x1={12} y1={18} x2={12} y2={22} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={8} y1={22} x2={16} y2={22} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
    </Svg>
  );
}

export function ChipIcon({ color, size = 19 }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Rect
        x={6}
        y={6}
        width={12}
        height={12}
        rx={2}
        stroke={color}
        strokeWidth={1.7}
        strokeLinejoin="round"
      />
      <Line x1={3} y1={9} x2={6} y2={9} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={3} y1={15} x2={6} y2={15} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={18} y1={9} x2={21} y2={9} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={18} y1={15} x2={21} y2={15} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={9} y1={3} x2={9} y2={6} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={15} y1={3} x2={15} y2={6} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={9} y1={18} x2={9} y2={21} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={15} y1={18} x2={15} y2={21} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
    </Svg>
  );
}

export function WaveformIcon({ color, size = 19 }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Line x1={4} y1={10} x2={4} y2={14} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={8} y1={6} x2={8} y2={18} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={12} y1={3} x2={12} y2={21} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={16} y1={6} x2={16} y2={18} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
      <Line x1={20} y1={10} x2={20} y2={14} stroke={color} strokeWidth={1.7} strokeLinecap="round" />
    </Svg>
  );
}

export function SparkleIcon({ color, size = 19 }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Path
        d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z"
        stroke={color}
        strokeWidth={1.7}
        strokeLinejoin="round"
      />
    </Svg>
  );
}

export function ChevronIcon({ color, size = 16 }: IconProps) {
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" fill="none">
      <Polyline
        points="9 6 15 12 9 18"
        stroke={color}
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Svg>
  );
}

const ConfigIcons = {
  microphone: MicrophoneIcon,
  chip: ChipIcon,
  waveform: WaveformIcon,
  sparkle: SparkleIcon,
} satisfies Record<ConfigIcon, (props: IconProps) => React.ReactElement>;

export function ConfigEntryIcon({ name, color, size }: IconProps & { name: ConfigIcon }) {
  const Icon = ConfigIcons[name];
  return <Icon color={color} size={size} />;
}
