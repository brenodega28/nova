import { useEffect } from 'react';
import { StyleSheet, View } from 'react-native';
import Animated, {
  cancelAnimation,
  Easing,
  interpolate,
  useAnimatedStyle,
  useSharedValue,
  withRepeat,
  withTiming,
} from 'react-native-reanimated';

import { Radius } from '@/constants/theme';
import { useTheme } from '@/hooks/use-theme';

const PULSE_DURATION = 1800;

export function AssistantAvatar({ pulsing }: { pulsing: boolean }) {
  const theme = useTheme();
  const progress = useSharedValue(0);

  useEffect(() => {
    if (!pulsing) {
      cancelAnimation(progress);
      progress.value = 0;
      return;
    }

    progress.value = 0;
    progress.value = withRepeat(
      withTiming(1, { duration: PULSE_DURATION, easing: Easing.inOut(Easing.ease) }),
      -1,
      false
    );

    return () => cancelAnimation(progress);
  }, [pulsing, progress]);

  const ringStyle = useAnimatedStyle(() => ({
    opacity: interpolate(progress.value, [0, 0.7, 1], [0.5, 0, 0]),
    transform: [{ scale: interpolate(progress.value, [0, 0.7, 1], [1, 1.55, 1.55]) }],
  }));

  const antenna = { backgroundColor: theme.accent };
  const eye = { backgroundColor: theme.onAccent };

  return (
    <View style={styles.container}>
      <View style={[styles.antenna, styles.antennaLeft, antenna]} />
      <View style={[styles.antenna, styles.antennaRight, antenna]} />

      <View style={[styles.body, { backgroundColor: theme.accent, shadowColor: theme.accent }]}>
        <View style={styles.eyes}>
          <View style={[styles.eye, eye]} />
          <View style={[styles.eye, eye]} />
        </View>
        <View style={styles.mouth}>
          <View style={[styles.mouthDot, eye]} />
          <View style={[styles.mouthDot, eye]} />
        </View>
      </View>

      <Animated.View
        pointerEvents="none"
        style={[styles.ring, { borderColor: theme.accent }, ringStyle]}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    width: 128,
    height: 124,
  },
  antenna: {
    position: 'absolute',
    top: 0,
    width: 14,
    height: 26,
    borderRadius: 7,
  },
  antennaLeft: {
    left: 28,
  },
  antennaRight: {
    right: 28,
  },
  body: {
    position: 'absolute',
    top: 24,
    left: 0,
    width: 128,
    height: 100,
    borderRadius: Radius.avatar,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 12,
    shadowOffset: { width: 0, height: 14 },
    shadowOpacity: 0.32,
    shadowRadius: 20,
    elevation: 10,
  },
  eyes: {
    flexDirection: 'row',
    gap: 26,
  },
  eye: {
    width: 10,
    height: 10,
    borderRadius: 5,
  },
  mouth: {
    flexDirection: 'row',
    gap: 6,
  },
  mouthDot: {
    width: 5,
    height: 5,
    borderRadius: 2.5,
    opacity: 0.85,
  },
  ring: {
    position: 'absolute',
    top: 24,
    left: 0,
    width: 128,
    height: 100,
    borderRadius: Radius.avatar,
    borderWidth: 2,
  },
});
