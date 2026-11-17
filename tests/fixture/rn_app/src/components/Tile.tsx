import React from 'react';
import { Pressable, Text } from 'react-native';

export default function Tile({ title, onPress }) {
  return <Pressable onPress={onPress} accessibilityLabel={title}><Text>{title}</Text></Pressable>;
}
